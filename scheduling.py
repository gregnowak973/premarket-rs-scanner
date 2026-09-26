"""Install or remove the background jobs.

1. Recorder: runs `app.py record` every 5 minutes, all day, every day. `record` exits
   right away when nothing is due, so this works in any time zone and catches up as
   soon as the computer wakes up.
2. Website: keeps `app.py serve --site-only` running from login, at http://localhost:8050.

    macOS    launchd agents in ~/Library/LaunchAgents/org.premarket-rs-scanner.*.plist
    Windows  Task Scheduler task "premarket-rs-scanner" + a script in your Startup folder
    Linux    two lines in your crontab (every 5 minutes, and @reboot)
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import shutil
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "app.py"
LOG = HERE / "data" / "record.log"
NAME = "premarket-rs-scanner"
LABEL = f"org.{NAME}.record"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SITE_LABEL = f"org.{NAME}.site"
SITE_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{SITE_LABEL}.plist"
SITE_LOG = HERE / "data" / "site.log"
STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"{NAME}-site.vbs"
URL = "http://127.0.0.1:8050"  # not "localhost": some Macs resolve that to IPv6 first
CRON_TAG = f"# {NAME}"


def python() -> str:
    exe = Path(sys.executable)
    if os.name == "nt":  # pythonw runs without flashing a console window
        w = exe.with_name("pythonw.exe")
        return str(w if w.exists() else exe)
    return str(exe)


def _plist(path: Path, label: str, args: list[str], schedule: str, log: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = "".join(f"<string>{a}</string>" for a in args)
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{argv}</array>
  <key>WorkingDirectory</key><string>{HERE}</string>
  {schedule}
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
""")
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(path)], check=True)


def _mac_install() -> None:
    _plist(PLIST, LABEL, [python(), str(APP), "record"],
           "<key>StartInterval</key><integer>300</integer>", LOG)
    _plist(SITE_PLIST, SITE_LABEL, [python(), str(APP), "serve", "--site-only"],
           "<key>KeepAlive</key><true/>", SITE_LOG)


def _mac_remove() -> None:
    for p in (PLIST, SITE_PLIST):
        subprocess.run(["launchctl", "unload", "-w", str(p)], capture_output=True)
        p.unlink(missing_ok=True)


def _win_install() -> None:
    # XML rather than plain schtasks flags so the task also runs on battery and
    # runs as soon as possible after a missed start (laptop asleep).
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2020-01-01T00:00:00</StartBoundary>
      <Repetition><Interval>PT5M</Interval></Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions>
    <Exec>
      <Command>{python()}</Command>
      <Arguments>"{APP}" record</Arguments>
      <WorkingDirectory>{HERE}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
        f.write(xml)
    try:
        subprocess.run(["schtasks", "/Create", "/TN", NAME, "/XML", f.name, "/F"], check=True)
    finally:
        os.unlink(f.name)
    # The website: a hidden launcher in the Startup folder (no admin rights needed).
    STARTUP.parent.mkdir(parents=True, exist_ok=True)
    STARTUP.write_text(f'CreateObject("WScript.Shell").Run """{python()}"" ""{APP}"" serve --site-only", 0, False\n')
    subprocess.Popen(["wscript", str(STARTUP)], cwd=HERE)


def _win_remove() -> None:
    subprocess.run(["schtasks", "/Delete", "/TN", NAME, "/F"], capture_output=True)
    STARTUP.unlink(missing_ok=True)
    print("If the website is still running, it stops at your next sign-out.")


def _crontab() -> list[str]:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if CRON_TAG not in l] if r.returncode == 0 else []


def _cron_write(lines: list[str]) -> None:
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


def _cron_install() -> None:
    if not shutil.which("crontab"):
        sys.exit("No crontab found. Install cron, or run `python app.py` in the background instead.")
    run = f'cd "{HERE}" && "{python()}" app.py'
    _cron_write(_crontab() + [f"*/5 * * * * {run} record {CRON_TAG}",
                              f'@reboot {run} serve --site-only >> "{SITE_LOG}" 2>&1 {CRON_TAG}'])
    subprocess.Popen(f'{run} serve --site-only >> "{SITE_LOG}" 2>&1', shell=True, start_new_session=True)


def _cron_remove() -> None:
    _cron_write(_crontab())


def protected_folder() -> str | None:
    """macOS blocks background jobs from these folders unless Python is given access."""
    if platform.system() != "Darwin":
        return None
    home = Path.home()
    for name in ("Documents", "Desktop", "Downloads", "Library/Mobile Documents"):
        if (home / name) in HERE.parents:
            return name
    return None


def site_up() -> bool:
    try:
        with urllib.request.urlopen(URL, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _tail(path: Path, n: int = 15) -> str:
    if not path.exists():
        return "  (no log yet)"
    return "\n".join("  " + l for l in path.read_text(errors="replace").splitlines()[-n:])


def status() -> None:
    print(f"Folder: {HERE}")
    if folder := protected_folder():
        print(f"PROBLEM: the folder is inside ~/{folder}; macOS stops background jobs from reading it.\n"
              f'  Fix: mv "{HERE}" ~/{NAME} && cd ~/{NAME} && {Path(python()).name} app.py schedule')
    print(f"Website {URL}: {'UP' if site_up() else 'NOT RESPONDING'}")
    if platform.system() == "Darwin":
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        jobs = [l for l in r.stdout.splitlines() if NAME in l]
        print("launchd jobs (PID, last exit code, name):")
        print("\n".join("  " + l for l in jobs) or "  none installed; run: python3 app.py schedule")
    print(f"Website log ({SITE_LOG.name}):\n{_tail(SITE_LOG)}")
    print(f"Recorder log ({LOG.name}):\n{_tail(LOG, 8)}")


def install() -> None:
    if folder := protected_folder():
        sys.exit(f"This folder is inside ~/{folder}, and macOS won't let background jobs read it.\n"
                 f"Move it to your home folder, then schedule again:\n\n"
                 f'  mv "{HERE}" ~/{NAME}\n  cd ~/{NAME}\n  {Path(python()).name} app.py schedule\n')
    LOG.parent.mkdir(exist_ok=True)
    system = platform.system()
    {"Darwin": _mac_install, "Windows": _win_install}.get(system, _cron_install)()
    print(f"Scheduled ({system}): the recorder runs every 5 minutes, and the website starts at login.")
    print(f"It records the morning snapshots on weekdays and fills in missed days on its next run. Log: {LOG}")
    print("Waiting for the website to start...", end="", flush=True)
    for _ in range(60):  # the first start can take a while as Python loads pandas
        if site_up():
            print(f" up.\nOpening {URL}")
            webbrowser.open(URL)
            return
        time.sleep(1)
        print(".", end="", flush=True)
    print(" it didn't start within a minute. Details:\n")
    status()


def remove() -> None:
    {"Darwin": _mac_remove, "Windows": _win_remove}.get(platform.system(), _cron_remove)()
    print("Removed the scheduled job.")
