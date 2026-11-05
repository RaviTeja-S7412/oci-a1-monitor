import argparse
import fcntl
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import oci
import settings

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / settings.STATE_PATH
LOCK_PATH = ROOT / "monitor.lock"
LOG_PATH = ROOT / "monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("oci-a1-monitor")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_state():
    if not STATE_PATH.exists():
        return {"status": "ready"}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Cannot parse {STATE_PATH}; refusing to launch to avoid a duplicate. "
            f"Inspect and repair the state file. Details: {exc}"
        )


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


class ProcessLock:
    """Linux process lock; GitHub Actions concurrency is an additional guard."""
    def __enter__(self):
        self.handle = open(LOCK_PATH, "w")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("Another monitor process is already running.")
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def make_clients():
    config_file = os.getenv("OCI_CONFIG_FILE", str(Path.home() / ".oci" / "config"))
    profile = os.getenv("OCI_CONFIG_PROFILE", "DEFAULT")
    config = oci.config.from_file(file_location=config_file, profile_name=profile)
    if config.get("region") != settings.REGION:
        raise RuntimeError(
            f"OCI config region is {config.get('region')!r}; expected {settings.REGION!r}."
        )
    return (
        oci.core.ComputeClient(config),
        oci.ons.NotificationDataPlaneClient(config),
    )


def validate_config():
    if not settings.COMPARTMENT_ID:
        raise RuntimeError("OCI_COMPARTMENT_ID environment variable is missing.")
    if not settings.NOTIFICATION_TOPIC_ID:
        raise RuntimeError("OCI_NOTIFICATION_TOPIC_ID environment variable is missing.")
    if not settings.SSH_PUBLIC_KEY.startswith("ssh-ed25519 "):
        raise RuntimeError("OCI_SSH_PUBLIC_KEY must contain an ssh-ed25519 public key.")
    if settings.REGION != "ap-hyderabad-1":
        raise RuntimeError("Region mismatch: this monitor is configured for Hyderabad.")
    if settings.SHAPE != "VM.Standard.A1.Flex" or settings.OCPUS != 2 or settings.MEMORY_GB != 12:
        raise RuntimeError("Unexpected compute configuration; review the Always Free target.")
    if settings.BOOT_VOLUME_SIZE_GB < 50:
        raise RuntimeError("OCI boot volume must be at least 50 GB.")


def send_notification(ons, title, body):
    message = oci.ons.models.MessageDetails(title=title, body=body)
    response = ons.publish_message(
        topic_id=settings.NOTIFICATION_TOPIC_ID,
        message_details=message,
        message_type="RAW_TEXT",
    )
    log.info("Notification published. Message ID: %s", response.data.message_id)


def list_a1_instances(compute):
    """Inspect all instances in the target AD; don't assume an unrelated A1 is ours."""
    response = oci.pagination.list_call_get_all_results(
        compute.list_instances,
        compartment_id=settings.COMPARTMENT_ID,
        availability_domain=settings.AVAILABILITY_DOMAIN,
    )
    return [
        inst for inst in response.data
        if inst.shape == settings.SHAPE
        and inst.lifecycle_state not in ("TERMINATED", "TERMINATING")
    ]


def launch_details():
    return oci.core.models.LaunchInstanceDetails(
        compartment_id=settings.COMPARTMENT_ID,
        availability_domain=settings.AVAILABILITY_DOMAIN,
        display_name=settings.INSTANCE_NAME,
        shape=settings.SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=settings.OCPUS,
            memory_in_gbs=settings.MEMORY_GB,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=settings.IMAGE_ID,
            boot_volume_size_in_gbs=settings.BOOT_VOLUME_SIZE_GB,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=settings.SUBNET_ID,
            assign_public_ip=settings.ASSIGN_PUBLIC_IP,
        ),
        metadata={"ssh_authorized_keys": settings.SSH_PUBLIC_KEY},
        freeform_tags={
            "managed-by": "oci-a1-monitor",
            "purpose": "a1-free-tier-provisioning",
        },
    )


def main():
    parser = argparse.ArgumentParser(description="One-shot OCI A1 monitor / provisioner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Validate; never launch")
    group.add_argument("--launch", action="store_true", help="Submit a real OCI launch request")
    args = parser.parse_args()

    with ProcessLock():
        validate_config()
        compute, ons = make_clients()
        state = load_state()

        if state.get("status") == "created":
            log.info(
                "Provisioning already succeeded (instance OCID %s). Nothing to do.",
                state.get("instance_id"),
            )
            return 0

        instances = list_a1_instances(compute)
        if instances:
            # An A1 already exists. Only call it ours if it carries our management tag.
            managed = next(
                (i for i in instances if (i.freeform_tags or {}).get("managed-by") == "oci-a1-monitor"),
                None,
            )
            if managed:
                state.update({
                    "status": "created",
                    "instance_id": managed.id,
                    "instance_name": managed.display_name,
                    "lifecycle_state": managed.lifecycle_state,
                    "discovered_at": utc_now(),
                })
                save_state(state)
                send_notification(
                    ons,
                    "OCI A1 instance detected",
                    f"Monitor-managed instance found.\nName: {managed.display_name}\n"
                    f"OCID: {managed.id}\nState: {managed.lifecycle_state}",
                )
                log.info("Found monitor-managed A1 instance; monitoring will stop.")
                return 0

            state.update({
                "status": "blocked_existing_a1",
                "existing_instance_ids": [i.id for i in instances],
                "last_checked_at": utc_now(),
            })
            save_state(state)
            log.warning("An A1 instance already exists in the target AD but is not tagged as monitor-managed.")
            send_notification(
                ons,
                "OCI A1 monitor paused: existing A1 found",
                "An A1 instance already exists in the configured availability domain, "
                "but it is not tagged as managed by this monitor. No launch was attempted. "
                "Review the OCI Console and state/monitor_state.json.",
            )
            return 0

        if args.dry_run:
            log.info("DRY RUN passed: OCI authentication, configuration and capacity checks completed. No launch submitted.")
            return 0

        if not settings.ENABLE_REAL_LAUNCH:
            raise RuntimeError(
                "Real launch is disabled. Set GitHub Secret ENABLE_REAL_LAUNCH to true "
                "only after reviewing the configuration and Always Free eligibility."
            )

        # Keep an idempotency token only while an outcome is ambiguous. A known
        # capacity/rate-limit response gets a fresh token on the next scheduled run.
        token = state.get("retry_token") or str(uuid.uuid4())
        state.update({
            "status": "launch_pending",
            "retry_token": token,
            "launch_attempt_started_at": utc_now(),
            "instance_name": settings.INSTANCE_NAME,
        })
        save_state(state)

        try:
            response = compute.launch_instance(launch_details(), opc_retry_token=token)
            inst = response.data
            state.update({
                "status": "created",
                "instance_id": inst.id,
                "instance_name": inst.display_name,
                "lifecycle_state": inst.lifecycle_state,
                "created_at": utc_now(),
                "retry_token": None,
            })
            save_state(state)
            send_notification(
                ons,
                "OCI A1 instance launch accepted",
                f"OCI accepted the instance launch request.\nName: {inst.display_name}\n"
                f"OCID: {inst.id}\nInitial state: {inst.lifecycle_state}\n"
                f"Region: {settings.REGION}\nCheck OCI Console for provisioning completion and public IP.",
            )
            log.info("Launch accepted. Subsequent scheduled runs will exit without launching.")
            return 0

        except oci.exceptions.ServiceError as exc:
            state.update({
                "status": "launch_error",
                "last_error_code": exc.code,
                "last_error_message": exc.message,
                "last_error_status": exc.status,
                "last_error_at": utc_now(),
            })
            # These are definitive responses; generate a new idempotency token next time.
            if exc.status == 429 or "capacity" in (exc.message or "").lower() or exc.code in (
                "TooManyRequests", "LimitExceeded"
            ):
                state["retry_token"] = None
                save_state(state)
                log.warning("Capacity/rate/limit response; no immediate retry. Next scheduled run will check again.")
            else:
                # Preserve token for review; don't automatically hammer on unknown errors.
                state["retry_token"] = token
                save_state(state)
                log.error("OCI launch error %s (HTTP %s): %s", exc.code, exc.status, exc.message)
            return 2

        except Exception:
            # A timeout or unexpected transport error may have an ambiguous outcome.
            # Preserve token; next run checks OCI for the instance before any retry.
            log.exception("Unexpected launch outcome. State/token retained for safe recovery.")
            raise


if __name__ == "__main__":
    raise SystemExit(main())
