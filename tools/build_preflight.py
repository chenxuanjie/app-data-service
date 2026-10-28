"""Check the actual Docker daemon's HTTPS proxy without exposing its URL/token."""

import subprocess
import sys
import urllib.request


def main():
    try:
        proxy = subprocess.check_output(
            ["docker", "info", "--format", "{{.HTTPSProxy}}"], text=True, timeout=60
        ).strip()
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy} if proxy else {})
        )
        url = (
            "https://auth.docker.io/token?service=registry.docker.io"
            "&scope=repository:library/python:pull"
        )
        with opener.open(url, timeout=60) as response:
            if response.status != 200:
                raise RuntimeError("Registry authentication unavailable")
        print("Docker HTTPS proxy / registry authentication endpoint passed.")
        return 0
    except Exception as error:
        print(f"Docker network preflight failed: {type(error).__name__}; check local proxy/WSL.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
