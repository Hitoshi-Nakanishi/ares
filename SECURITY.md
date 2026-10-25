# Security

Security fixes target the current `main` branch. Older snapshots do not have a separate support commitment.

Ares executes trusted local profiles, engines and models. Configuration is operator-controlled, and Docker access is a
privileged host capability. Ares is not a sandbox for untrusted code. A loopback listener does not authenticate other local
users. Host reserves and memory supervision reduce resource pressure; they do not make a model or engine safe to execute.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting from the repository's Security tab when available. Otherwise, open an issue
requesting a confidential contact channel, without exploit details, confidential files or credentials. Wait for a private
channel before sharing sensitive material. Do not post real account information, access tokens or proprietary examples.
