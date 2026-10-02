import json
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone

# gufo inference engine container (see docs/technical/AI-BACKEND.md).
# Managed outside the dashboard via docker; the dashboard only starts/stops it.
LLM_CONTAINER = "gufo-flashnext"
LLM_API_PORT = 8080
LLM_MODEL_ID = "qwen3.8-flash-next"
LLM_STOP_TIMEOUT = 60  # seconds; unloading a ~94 GiB resident model is not instant


class LLMController:
    """Start/stop control for the local gufo LLM container."""

    def __init__(self, notifier):
        self.notifier = notifier
        self.available = self.check_available()

    def refresh_availability(self):
        self.available = self.check_available()
        return self.available

    def check_available(self):
        if not shutil.which("docker"):
            return False
        result = self._run_docker(
            ["inspect", "-f", "{{.State.Running}}", LLM_CONTAINER], timeout=5
        )
        return bool(result and result.returncode == 0)

    def _run_docker(self, args, timeout=15):
        """Run docker directly, then via passwordless sudo (mirrors power_controller)."""
        last_result = None
        for cmd in (["docker"] + args, ["sudo", "-n", "docker"] + args):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout
                )
            except Exception:
                continue
            last_result = result
            if result.returncode == 0:
                return result
        return last_result

    def _result_error(self, result):
        if result is None:
            return "Unable to execute docker"
        return result.stderr.strip() or result.stdout.strip() or "Unknown error"

    def is_running(self):
        result = self._run_docker(
            ["inspect", "-f", "{{.State.Running}}", LLM_CONTAINER], timeout=5
        )
        if result and result.returncode == 0:
            return result.stdout.strip().lower() == "true"
        return False

    def get_health(self):
        """Return (ok, detail) from the engine's /health endpoint."""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{LLM_API_PORT}/health", timeout=2
            ) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
                return body.get("status") == "ok", body.get("status", "")
        except Exception:
            return False, ""

    def get_status_text(self):
        if not self.available:
            return "gufo container not found"
        if not self.is_running():
            return "Stopped"
        ok, _ = self.get_health()
        return f"Serving · :{LLM_API_PORT}" if ok else f"Loading · :{LLM_API_PORT}"

    def _action(self, verb, past, args, timeout):
        if not self.available:
            self.notifier.notify(
                "AI Engine",
                f"Container {LLM_CONTAINER} not found on this device.",
                "warning",
                4000,
            )
            return False
        self.notifier.notify(
            "AI Engine", f"{verb} {LLM_MODEL_ID}…", "info", 2000
        )
        result = self._run_docker(args, timeout=timeout)
        if result and result.returncode == 0:
            self.notifier.notify(
                "AI Engine",
                f"{LLM_MODEL_ID} {past}",
                "success",
                3000,
            )
            return True
        self.notifier.notify_error("AI Engine", self._result_error(result))
        return False

    def start(self):
        return self._action(
            "Starting", "started", ["start", LLM_CONTAINER], timeout=120
        )

    def stop(self):
        return self._action(
            "Stopping",
            "stopped",
            ["stop", "-t", str(LLM_STOP_TIMEOUT), LLM_CONTAINER],
            timeout=180,
        )

    def restart(self):
        return self._action(
            "Restarting",
            "restarted",
            ["restart", "-t", str(LLM_STOP_TIMEOUT), LLM_CONTAINER],
            timeout=300,
        )

    def get_logs(self, lines=40):
        result = self._run_docker(
            ["logs", "--tail", str(lines), LLM_CONTAINER], timeout=10
        )
        if result and result.returncode == 0:
            return (result.stdout or "") + (result.stderr or "")
        return ""

    # ------------------------------------------------------------------
    # Runtime metrics (call from a worker thread; includes docker sampling)
    # ------------------------------------------------------------------
    def _http_json(self, path, timeout=3):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{LLM_API_PORT}{path}", timeout=timeout
            ) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return None

    def get_metrics(self):
        """Collect a metrics snapshot: engine endpoints + docker sampling.

        Blocking (docker stats samples ~2s) — call from a background thread.
        """
        out = {
            "running": self.is_running(),
            "model": LLM_MODEL_ID,
            "context_length": None,
            "status": "stopped",
            "processing": None,
            "deferred": None,
            "kv_ratio": None,
            "prompt_total": None,
            "predict_total": None,
            "prompt_tps": None,
            "predict_tps": None,
            "cpu_pct": None,
            "mem_used": None,
            "uptime_secs": None,
            "restarts": None,
        }
        if not out["running"]:
            return out

        ok, _ = self.get_health()
        out["status"] = "serving" if ok else "loading"

        models = self._http_json("/v1/models")
        try:
            entry = models["data"][0]
            out["model"] = entry.get("id", LLM_MODEL_ID)
            out["context_length"] = entry.get("context_length")
        except Exception:
            pass

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{LLM_API_PORT}/metrics", timeout=3
            ) as resp:
                for line in resp.read().decode("utf-8", "replace").splitlines():
                    if line.startswith("#") or " " not in line:
                        continue
                    key, _, val = line.partition(" ")
                    try:
                        num = float(val)
                    except ValueError:
                        continue
                    if key.endswith("requests_processing"):
                        out["processing"] = int(num)
                    elif key.endswith("requests_deferred"):
                        out["deferred"] = int(num)
                    elif key.endswith("kv_cache_usage_ratio"):
                        out["kv_ratio"] = num
                    elif key.endswith("prompt_tokens_total"):
                        out["prompt_total"] = int(num)
                    elif key.endswith("tokens_predicted_total"):
                        out["predict_total"] = int(num)
                    elif key.endswith("prompt_tokens_seconds"):
                        out["prompt_tps"] = num
                    elif key.endswith("predicted_tokens_seconds"):
                        out["predict_tps"] = num
        except Exception:
            pass

        stats = self._run_docker(
            [
                "stats", "--no-stream",
                "--format", "{{.CPUPerc}}\t{{.MemUsage}}",
                LLM_CONTAINER,
            ],
            timeout=10,
        )
        if stats and stats.returncode == 0:
            parts = stats.stdout.strip().split("\t")
            if len(parts) == 2:
                out["cpu_pct"] = parts[0].strip()
                out["mem_used"] = parts[1].split("/")[0].strip()

        insp = self._run_docker(
            ["inspect", "-f", "{{.State.StartedAt}}\t{{.RestartCount}}", LLM_CONTAINER],
            timeout=5,
        )
        if insp and insp.returncode == 0:
            parts = insp.stdout.strip().split("\t")
            try:
                started = datetime.fromisoformat(
                    parts[0].replace("Z", "+00:00")
                )
                out["uptime_secs"] = max(
                    0, int((datetime.now(timezone.utc) - started).total_seconds())
                )
                out["restarts"] = int(parts[1])
            except Exception:
                pass

        return out
