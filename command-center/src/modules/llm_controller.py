import json
import shutil
import subprocess
import urllib.request

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
