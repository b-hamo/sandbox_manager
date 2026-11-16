"""Management projection limits, shared by packaging and publication.

Keep aligned with micro_VM's management_policy.hpp. Inbox downloads and
artifact exports have separate limits and are not changed by these values.
"""

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_SESSION_BYTES = 2 * 1024 * 1024 * 1024


def validate_service_limits(result: dict) -> dict:
    """Require the running worker's contract, not an adjacent metadata file."""
    limits = result.get("transfer_limits", {}) if isinstance(result, dict) else {}
    expected = {"max_file_bytes": MAX_FILE_BYTES, "max_session_bytes": MAX_SESSION_BYTES}
    if (not isinstance(result, dict) or not isinstance(limits, dict) or
            result.get("protocol") != "minobox-management-v1" or
            any(type(limits.get(k)) is not int or limits[k] != v for k, v in expected.items())):
        raise ValueError("Management transfer contract mismatch: install the matching Manager and "
                         "512 MiB / 2 GiB management service during an idle maintenance window; existing sessions preserved")
    return limits


def check_service(executable):
    """Read-only health query. Never starts/stops a service or creates a lease."""
    import json
    import subprocess
    import uuid
    request = {"protocol": "minobox-management-v1", "request_id": str(uuid.uuid4()), "op": "health", "payload": {}}
    reply = subprocess.run([str(executable), "--client"], input=json.dumps(request).encode(),
                           capture_output=True, timeout=45,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = json.loads(reply.stdout.decode("utf-8-sig"))
    if reply.returncode or not isinstance(data, dict) or data.get("ok") is not True:
        raise ValueError("Management health refused; installation not verified and existing sessions preserved")
    return validate_service_limits(data.get("result", {}))


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Verify installed management transfer limits without changing sessions")
    parser.add_argument("--service", required=True)
    args = parser.parse_args()
    print(json.dumps(check_service(args.service)))
