from gi.repository import Gio, GLib

# GNOME/mutter display control via the org.gnome.Mutter.DisplayConfig D-Bus API.
# Works on any GNOME Wayland session without extra tooling; refresh-rate
# switches take effect instantly and persist (method=2) in the session.
_DBUS_NAME = "org.gnome.Mutter.DisplayConfig"
_DBUS_PATH = "/org/gnome/Mutter/DisplayConfig"


class DisplayController:
    """Refresh-rate control for the built-in panel (GNOME DisplayConfig)."""

    def __init__(self, notifier):
        self.notifier = notifier
        self._proxy = None
        self._serial = 0
        self.available = self.check_available()

    def _get_proxy(self):
        if self._proxy is None:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._proxy = Gio.DBusProxy.new_sync(
                bus,
                Gio.DBusProxyFlags.NONE,
                None,
                _DBUS_NAME,
                _DBUS_PATH,
                _DBUS_NAME,
                None,
            )
        return self._proxy

    def check_available(self):
        try:
            proxy = self._get_proxy()
            result = proxy.call_sync(
                "GetCurrentState",
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            return result is not None
        except Exception:
            return False

    def refresh_availability(self):
        self.available = self.check_available()
        return self.available

    def get_state(self):
        """Return (panel_connector, modes, logical_monitors, serial).

        modes: list of dicts {id, w, h, rate, is_current, is_preferred, vrr}
        """
        proxy = self._get_proxy()
        result = proxy.call_sync(
            "GetCurrentState", None, Gio.DBusCallFlags.NONE, 3000, None
        )
        serial, monitors, logical, _props = result.unpack()
        self._serial = serial

        panel = None
        modes = []
        for monitor in monitors:
            info, monitor_modes, _mp = monitor
            connector = info[0]
            if connector.startswith("eDP"):
                panel = connector
                for m in monitor_modes:
                    mode_id, w, h, rate, _pref_scale, _scales, flags = m
                    is_current = bool(flags.get("is-current", False))
                    modes.append(
                        {
                            "id": mode_id,
                            "w": w,
                            "h": h,
                            "rate": round(float(rate)),
                            "is_current": is_current,
                            "is_preferred": bool(flags.get("is-preferred", False)),
                            "vrr": "+vrr" in mode_id,
                        }
                    )
                break
        return panel, modes, logical, serial

    def get_refresh_rates(self):
        """Unique refresh rates available at the current resolution."""
        try:
            panel, modes, _logical, _serial = self.get_state()
            if panel is None:
                return []
            current = next((m for m in modes if m["is_current"]), None)
            if current is None:
                return []
            rates = sorted(
                {m["rate"] for m in modes if (m["w"], m["h"]) == (current["w"], current["h"])},
                reverse=True,
            )
            return rates
        except Exception:
            return []

    def get_current_rate(self):
        try:
            _panel, modes, _logical, _serial = self.get_state()
            current = next((m for m in modes if m["is_current"]), None)
            return current["rate"] if current else None
        except Exception:
            return None

    def set_refresh(self, rate):
        """Switch the panel to `rate` Hz at its current resolution."""
        try:
            panel, modes, logical, serial = self.get_state()
            if panel is None or not logical:
                return False
            current = next((m for m in modes if m["is_current"]), None)
            if current is None:
                return False

            candidates = [
                m
                for m in modes
                if (m["w"], m["h"]) == (current["w"], current["h"])
                and m["rate"] == int(rate)
            ]
            if not candidates:
                self.notifier.notify(
                    "Display", f"{rate} Hz not available on this panel", "warning", 3000
                )
                return False
            # Prefer the fixed (non-VRR) variant; fall back to VRR-only rates.
            target = next((m for m in candidates if not m["vrr"]), candidates[0])

            lm = logical[0]
            x, y, scale, transform, primary = lm[0], lm[1], lm[2], lm[3], lm[4]
            # This mutter's logical-monitor entry carries a(ssa{sv}) monitor
            # specs: [(connector, monitor_mode_id, properties)].
            monitor_spec = (panel, target["id"], {})
            lm_tuple = (x, y, scale, transform, primary, [monitor_spec])
            args = GLib.Variant(
                "(uua(iiduba(ssa{sv}))a{sv})", (serial, 2, [lm_tuple], {})
            )
            proxy = self._get_proxy()
            proxy.call_sync(
                "ApplyMonitorsConfig",
                args,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            self.notifier.notify(
                "Display",
                f"Panel set to {rate} Hz",
                "success",
                2000,
            )
            return True
        except Exception as e:
            self.notifier.notify_error("Display Refresh", str(e))
            return False
