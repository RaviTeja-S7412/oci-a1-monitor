# OCI A1 Capacity Monitor

This private-repository workflow checks OCI A1 capacity every six hours and attempts one provisioning request per run until an instance is accepted or an existing A1 is detected.

## Safety

- The workflow uses OCI API credentials stored in GitHub Actions Secrets.
- Never commit OCI private keys, OCI config files, or `.env` files.
- `ENABLE_REAL_LAUNCH` must be the GitHub Secret value `true` before real launch requests are allowed.
- Verify Always Free eligibility and billing details in OCI Console before enabling launches.
- State is persisted in `state/monitor_state.json`; do not delete or reset it casually.
- GitHub Actions scheduled runs use UTC: 00:00, 06:00, 12:00, 18:00 UTC (05:30, 11:30, 17:30, 23:30 IST).

## Required GitHub Actions Secrets

- `OCI_USER_OCID`
- `OCI_TENANCY_OCID`
- `OCI_FINGERPRINT`
- `OCI_PRIVATE_KEY` (full PEM content, preserving line breaks)
- `OCI_REGION` (`ap-hyderabad-1`)
- `OCI_COMPARTMENT_ID` (root compartment / tenancy OCID)
- `OCI_NOTIFICATION_TOPIC_ID`
- `OCI_SSH_PUBLIC_KEY` (public key only, usually starts with `ssh-ed25519`)
- `ENABLE_REAL_LAUNCH` (`false` for initial validation; change to `true` only after dry-run verification)

## Manual local validation

Install dependencies and configure a standard OCI config at `~/.oci/config`, then set the required environment variables and run:

```bash
python monitor.py --dry-run
```

The GitHub workflow runs `python monitor.py --launch` after secrets are configured.
