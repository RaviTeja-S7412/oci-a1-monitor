OCI A1 Monitor — Windows

1) Copy this folder's files into F:\oci-a1-monitor (keep your existing venv and ~/.oci files).
2) Activate your venv:
   .\venv\Scripts\activate
3) Install/update dependency:
   python -m pip install -r requirements.txt
4) Edit settings.py:
   - COMPARTMENT_ID: your tenancy OCID (the root compartment OCID from ~/.oci/config is config["tenancy"])
   - NOTIFICATION_TOPIC_ID: your OCI Notifications topic OCID
   - Leave ENABLE_REAL_LAUNCH = False for now.
5) Dry run:
   python monitor.py --dry-run

IMPORTANT SAFETY:
- A dry run does not create any OCI resources.
- Do not set ENABLE_REAL_LAUNCH=True until the user has reviewed the launch summary
  and verified Always Free eligibility in OCI Console.
- OCI quotas do not guarantee physical A1 capacity or Always Free billing treatment.
- This program only launches the specified A1 shape, 2 OCPU / 12 GB, 50 GB boot volume.
- It does not create paid fallback resources.
- A launch request is a real cloud action and may incur charges if OCI marks a resource
  as chargeable or if account allowances are exceeded.
- The script checks for an existing instance with the exact configured display name
  before submitting a launch request.
- Keep monitor_state.json and monitor.log. Do not delete them while an attempt is pending.
- If a run times out or has an ambiguous result, the next run checks OCI for the instance
  first and reuses the persisted retry token.
- A single process lock prevents overlapping Task Scheduler runs.
- Each run performs one check and exits; schedule it every 6 hours with Windows Task Scheduler.
- Notification emails are sent through OCI Notifications; no Gmail credentials are used.

Task Scheduler:
Program:
  F:\oci-a1-monitor\venv\Scripts\python.exe
Arguments:
  F:\oci-a1-monitor\monitor.py --dry-run
Start in:
  F:\oci-a1-monitor

After explicit approval to enable real provisioning:
  - change ENABLE_REAL_LAUNCH = True
  - change the scheduled argument from --dry-run to --launch
  - run one manual --launch only after confirming the final configuration.
