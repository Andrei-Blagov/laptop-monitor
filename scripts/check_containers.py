"""Image verification and isolated real PID-namespace/volume acceptance.

Requires a previously built release image. Uses no Compose, .env or host data.
Every container has --network none. Temporary containers/volume are removed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args(argv)
    output = ROOT / "dist" / "checks"
    output.mkdir(parents=True, exist_ok=True)
    prefix = "laptop-f2-" + uuid.uuid4().hex[:12]
    holder = prefix + "-holder"
    volume = prefix + "-queue"
    evidence = {"image": args.image, "ok": False,
                "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()}
    if shutil.which("docker") is None:
        evidence["unavailable"] = "Docker executable is not installed"
        (output / "containers-report.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(evidence["unavailable"], file=sys.stderr)
        return 2

    def docker(*command, check=True):
        proc = subprocess.run(["docker", *command], text=True, capture_output=True, timeout=150)
        if check and proc.returncode:
            raise RuntimeError(f"docker {command[0]} failed: {proc.stderr[-3000:]}")
        return proc.stdout.strip()

    common = ["--network", "none", "--mount", f"type=volume,src={volume},dst=/queue",
              "--mount", f"type=bind,src={ROOT / 'tests'},dst=/checks,readonly",
              "--env", "PYTHONPATH=/app:/checks/offline_guard",
              "--env", "LAPTOP_MONITOR_INSTANCE=f2-container-test",
              "--env", "STAGE22_MARKET_TELEGRAM=0"]

    def read(name):
        return json.loads(docker("run", "--rm", *common, args.image, "python", "-c",
                                f"from pathlib import Path; print(Path('/queue/{name}.json').read_text())"))

    try:
        with (output / "verify-image.log").open("w") as log:
            subprocess.run([sys.executable, "-m", "scripts.release_verify", "--image", args.image],
                           cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=320)
        evidence["image_verified"] = True
        metadata = json.loads(docker("image", "inspect", args.image))[0]
        evidence["image_id"] = metadata["Id"]
        evidence["image_user"] = metadata["Config"]["User"]
        docker("volume", "create", volume)
        docker("run", "--rm", "--user", "root", *common, args.image,
               "chown", "10001:10001", "/queue")
        docker("run", "--detach", "--name", holder, *common, args.image,
               "python", "/checks/cross_container_lease.py", "hold")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = docker("exec", holder, "python", "-c",
                           "from pathlib import Path; print(Path('/queue/ready').exists())")
            if ready == "True":
                break
            time.sleep(0.2)
        else:
            raise AssertionError("holder not ready: " + docker("logs", holder, check=False))
        info = json.loads(docker("inspect", holder))[0]
        assert info["HostConfig"]["NetworkMode"] == "none"
        assert info["HostConfig"]["PidMode"] == "", "must use separate PID namespaces"
        evidence["holder_isolated"] = True
        docker("run", "--rm", *common, args.image, "python", "/checks/cross_container_lease.py", "second")
        second = read("second")
        assert second["scan_id"] == "live-scan", second
        assert second["joined_folder"] == "processing", second
        assert sorted(second["event_ids"]) == ["held", "joined"], second
        assert second["results"] == [], second
        evidence["live_owner"] = second
        docker("kill", "--signal", "KILL", holder)
        docker("run", "--rm", *common, args.image, "python", "/checks/cross_container_lease.py", "recover")
        recovered = read("recovered")
        assert sorted(recovered["recovered"]) == ["held", "joined"], recovered
        evidence["after_kill"] = recovered
        docker("run", "--rm", *common, args.image, "python", "/checks/cross_container_lease.py", "finish")
        finished = read("finished")
        assert finished["passes"] == 1, finished
        assert finished["archived"] == ["held", "joined"], finished
        assert len(finished["sent"]) == 1, finished
        assert finished["again"] is None, finished
        evidence["finished"] = finished
        docker("rm", "--force", holder)
        docker("run", "--detach", "--name", holder, *common, args.image,
               "python", "/checks/cross_container_lease.py", "hold-delivery")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = docker("exec", holder, "python", "-c",
                           "from pathlib import Path; print(Path('/queue/delivery-ready').exists())")
            if ready == "True":
                break
            time.sleep(0.2)
        else:
            raise AssertionError("delivery holder not ready: " + docker("logs", holder, check=False))
        docker("run", "--rm", *common, args.image, "python", "/checks/cross_container_lease.py", "probe-delivery")
        docker("kill", "--signal", "KILL", holder)
        docker("run", "--rm", *common, args.image, "python", "/checks/cross_container_lease.py", "recover-delivery")
        evidence["delivery_after_kill"] = read("delivery-recovered")
        evidence["ok"] = True
        print("PASS: image, shared leases, live owner, kill recovery, one delivery and idle wake")
        return 0
    except Exception as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        print(evidence["error"], file=sys.stderr)
        return 1
    finally:
        docker("rm", "--force", holder, check=False)
        docker("volume", "rm", volume, check=False)
        (output / "containers-report.json").write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
