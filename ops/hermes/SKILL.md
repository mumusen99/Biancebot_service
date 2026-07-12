# Trading Operator Skill

1. Run `scripts/trading-status` before any change.
2. Prefer editing `config_runtime/runtime.yaml`; do not edit exchange credentials.
3. Candidate config version must increase and remain within `hard_limits.yaml`.
4. Run `scripts/trading-code-check` before applying any code change.
5. Do not modify order execution, stop-loss, locking, reconciliation, or credential code automatically.
6. Apply runtime parameters with `scripts/trading-config-apply candidate.yaml`.
7. Existing positions keep their original plan; runtime changes affect new entries only.
8. Never start legacy scripts under `archive/legacy_entries`.
