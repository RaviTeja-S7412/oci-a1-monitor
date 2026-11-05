"""Configuration for the OCI A1 monitor.

Non-secret infrastructure values are kept here. Credentials and sensitive values
are supplied through environment variables by GitHub Actions (or locally).
"""
import os

REGION = os.getenv("OCI_REGION", "ap-hyderabad-1")
COMPARTMENT_ID = os.getenv("OCI_COMPARTMENT_ID", "")
NOTIFICATION_TOPIC_ID = os.getenv("OCI_NOTIFICATION_TOPIC_ID", "")
AVAILABILITY_DOMAIN = "pyDN:AP-HYDERABAD-1-AD-1"

INSTANCE_NAME = "oci-a1-free-server"
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 2
MEMORY_GB = 12

IMAGE_ID = (
    "ocid1.image.oc1.ap-hyderabad-1."
    "aaaaaaaamvzbsljz2ja6iegrtbe2hak37xuahdhb6uylqnw37iyq7zgvrjqq"
)
SUBNET_ID = (
    "ocid1.subnet.oc1.ap-hyderabad-1."
    "aaaaaaaa2meflshyxb2lmo6fsnhlvv266ue2z5kdso7i27eewnl75h2hyj3a"
)

BOOT_VOLUME_SIZE_GB = 50
ASSIGN_PUBLIC_IP = True

# The workflow supplies the public SSH key as a GitHub Secret.
SSH_PUBLIC_KEY = os.getenv("OCI_SSH_PUBLIC_KEY", "").strip()

# Keep false until GitHub Secrets are configured and a dry run succeeds.
ENABLE_REAL_LAUNCH = os.getenv("ENABLE_REAL_LAUNCH", "false").lower() == "true"

# State is committed to the private repository by the workflow.
STATE_PATH = os.getenv("OCI_MONITOR_STATE_PATH", "state/monitor_state.json")
