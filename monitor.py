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
STATE_PATH = Path(settings.STATE_PATH)
if not STATE_PATH.is_absolute():
    STATE_PATH = ROOT / STATE_PATH
LOCK_PATH = ROOT / "monitor.lock"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("oci-a1-monitor")

# TERMINATING is transitional; only TERMINATED is treated as final.
TERMINAL_STATES = {"TERMINATED"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_state():
    if not STATE_PATH.exists():
        return {"status": "ready"}

    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Cannot parse {STATE_PATH}; refusing to launch to avoid a duplicate. "
            f"Inspect and repair the state file. Details: {exc}"
        ) from exc

    if not isinstance(state, dict) or not isinstance(state.get("status"), str):
        raise RuntimeError("Monitor state has an invalid structure; refusing to launch.")

    allowed = {
        "ready",
        "launch_pending",
        "launch_unknown",
        "created",
        "blocked_existing_a1",
        "launch_error",
        "terminal_instance",
    }
    if state["status"] not in allowed:
        raise RuntimeError(
            f"Unknown monitor state {state['status']!r}; refusing to launch."
        )
    return state


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
        except OSError as exc:
            self.handle.close()
            raise RuntimeError("Another monitor process is already running.") from exc
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
    return oci.core.ComputeClient(config), oci.ons.NotificationDataPlaneClient(config)


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
    if settings.BOOT_VOLUME_SIZE_GB != 50:
        raise RuntimeError("Expected a 50 GB boot volume; review the Always Free target.")
    if not settings.IMAGE_ID or not settings.SUBNET_ID or not settings.AVAILABILITY_DOMAIN:
        raise RuntimeError("Image, subnet, and availability domain must be configured.")


def send_notification(ons, title, body):
    message = oci.ons.models.MessageDetails(title=title, body=body)
    response = ons.publish_message(
        topic_id=settings.NOTIFICATION_TOPIC_ID,
        message_details=message,
        message_type="RAW_TEXT",
    )
    log.info("Notification published. Message ID: %s", response.data.message_id)


def queue_notification(state, title, body):
    state["pending_notification"] = {
        "title": title,
        "body": body,
        "queued_at": utc_now(),
    }


def deliver_pending_notification(ons, state):
    pending = state.get("pending_notification")
    if not pending:
        return
    try:
        send_notification(ons, pending["title"], pending["body"])
    except Exception:
        # Keep the message in state so a later scheduled run can retry it.
        log.exception("Notification delivery failed; it will be retried on a later run.")
        return
    state.pop("pending_notification", None)
    save_state(state)


def list_a1_instances(compute):
    response = oci.pagination.list_call_get_all_results(
        compute.list_instances,
        compartment_id=settings.COMPARTMENT_ID,
        availability_domain=settings.AVAILABILITY_DOMAIN,
    )
    return [
        inst for inst in response.data
        if inst.shape == settings.SHAPE and inst.lifecycle_state not in TERMINAL_STATES
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


def record_instance(compute, ons, state, instance, source):
    state.update({
        "status": "created",
        "instance_id": instance.id,
        "instance_name": instance.display_name,
        "lifecycle_state": instance.lifecycle_state,
        "last_lifecycle_check_at": utc_now(),
        "retry_token": None,
    })
    state.pop("launch_error", None)
    queue_notification(
        state,
        "OCI A1 instance detected" if source == "discovered" else "OCI A1 instance launch accepted",
        (
            f"Monitor-managed OCI A1 instance {source}.\n"
            f"Name: {instance.display_name}\n"
            f"OCID: {instance.id}\n"
            f"Initial/current state: {instance.lifecycle_state}\n"
            f"Region: {settings.REGION}\n"
            "The monitor will not submit another launch request."
        ),
    )
    save_state(state)
    log.info(
        "Instance %s; lifecycle state is %s. Future runs will monitor this OCID only.",
        instance.id,
        instance.lifecycle_state,
    )


def monitor_saved_instance(compute, ons, state):
    instance_id = state.get("instance_id")
    if not instance_id:
        raise RuntimeError("State says an instance was created but has no instance OCID.")

    try:
        instance = compute.get_instance(instance_id).data
    except oci.exceptions.ServiceError as exc:
        state["last_lifecycle_check_at"] = utc_now()
        state["last_lifecycle_error"] = f"{exc.code}: {exc.message}"
        save_state(state)
        log.error(
            "Could not retrieve saved instance %s. No new launch will be attempted: %s",
            instance_id,
            exc,
        )
        return

    state.update({
        "status": "created",
        "instance_name": instance.display_name,
        "lifecycle_state": instance.lifecycle_state,
        "last_lifecycle_check_at": utc_now(),
    })

    if instance.lifecycle_state in TERMINAL_STATES:
        state["status"] = "terminal_instance"
        state["terminal_at"] = utc_now()
        queue_notification(
            state,
            "OCI A1 instance entered a terminal state",
            f"Instance {instance.id} is {instance.lifecycle_state}. "
            "Automatic relaunch is disabled to prevent duplicate or unexpected resources. "
            "Review the OCI Console before changing monitor state.",
        )
    save_state(state)
    log.info(
        "Monitoring existing instance %s: lifecycle state=%s. No launch request will be made.",
        instance.id,
        instance.lifecycle_state,
    )


def main():
    parser = argparse.ArgumentParser(description="OCI A1 monitor / provisioner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Validate and inspect; never launch")
    group.add_argument("--launch", action="store_true", help="Submit a real OCI launch request")
    args = parser.parse_args()

    with ProcessLock():
        validate_config()
        compute, ons = make_clients()
        state = load_state()

        # Deliver a previously queued email independently of provisioning.
        deliver_pending_notification(ons, state)

        if state["status"] in {"created", "terminal_instance"}:
            monitor_saved_instance(compute, ons, state)
            return 0

        if state["status"] == "blocked_existing_a1":
            log.warning("Provisioning remains blocked by a previously detected unrelated A1 instance.")
            return 0

        # Always inspect OCI before deciding how to handle an ambiguous launch.
        # The original request may have succeeded even if its response was lost.
        instances = list_a1_instances(compute)
        if instances:
            managed = next(
                (i for i in instances if (i.freeform_tags or {}).get("managed-by") == "oci-a1-monitor"),
                None,
            )
            if managed:
                record_instance(compute, ons, state, managed, "discovered")
                return 0

            if state["status"] == "launch_unknown":
                log.error(
                    "Previous launch outcome is ambiguous and no monitor-tagged A1 "
                    "instance is currently visible. Automatic launch remains paused; "
                    "reconcile in OCI Console before resetting state."
                )
                return 2

            state.update({
                "status": "blocked_existing_a1",
                "existing_instance_ids": [i.id for i in instances],
                "last_checked_at": utc_now(),
            })
            queue_notification(
                state,
                "OCI A1 monitor paused: existing A1 found",
                "An A1 instance already exists in the configured availability domain, "
                "but it is not tagged as managed by this monitor. No launch was attempted. "
                "Review the OCI Console and state/monitor_state.json.",
            )
            save_state(state)
            log.warning("Unmanaged A1 instance found; provisioning is blocked.")
            return 0

        if state["status"] == "launch_unknown":
            log.error(
                "Previous launch outcome is ambiguous and no monitor-tagged A1 "
                "instance is currently visible. Automatic launch remains paused; "
                "reconcile in OCI Console before resetting state."
            )
            return 2

        if args.dry_run:
            log.info(
                "DRY RUN passed: OCI authentication, configuration and instance discovery "
                "completed. OCI capacity is not reserved or guaranteed. No launch submitted."
            )
            return 0

        if not settings.ENABLE_REAL_LAUNCH:
            raise RuntimeError(
                "Real launch is disabled. Set GitHub Secret ENABLE_REAL_LAUNCH to true "
                "only after reviewing the configuration and Always Free eligibility."
            )

        # A previous launch_pending state is treated as ambiguous and is never blindly resubmitted.
        if state["status"] == "launch_pending":
            state["status"] = "launch_unknown"
            state["last_recovery_check_at"] = utc_now()
            save_state(state)
            log.error(
                "Found launch_pending without a confirmed instance. Pausing for manual "
                "reconciliation; no second launch request will be submitted."
            )
            return 2

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
            record_instance(compute, ons, state, response.data, "accepted")
            return 0

        except oci.exceptions.ServiceError as exc:
            state.update({
                "last_error_code": exc.code,
                "last_error_message": exc.message,
                "last_error_status": exc.status,
                "last_error_at": utc_now(),
            })

            if (
                exc.status == 429
                or "capacity" in (exc.message or "").lower()
                or exc.code in ("TooManyRequests", "LimitExceeded")
            ):
                state.update({"status": "ready", "retry_token": None})
                save_state(state)
                log.warning(
                    "Definitive capacity/rate/limit response. No immediate retry; "
                    "the next scheduled run will check again."
                )
                return 0
            else:
                # Treat every other service error conservatively until reconciled.
                state["status"] = "launch_unknown"
                state["retry_token"] = token
                save_state(state)
                log.error(
                    "OCI returned an unexpected launch error %s (HTTP %s): %s. "
                    "Automatic retries are paused to prevent duplicates.",
                    exc.code,
                    exc.status,
                    exc.message,
                )
                return 2

        except Exception:
            state["status"] = "launch_unknown"
            state["retry_token"] = token
            state["last_error_at"] = utc_now()
            save_state(state)
            log.exception(
                "Unexpected launch outcome. State/token retained; automatic retries paused."
            )
            raise


if __name__ == "__main__":
    raise SystemExit(main())
