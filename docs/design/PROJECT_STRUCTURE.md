# Optimized Production Structure

- `main.py` / `app/main.py`: only production entry; one process schedules position management and strategy scans.
- `config_runtime/`: Hermes-editable runtime parameters and root-owned hard limits.
- `scripts/`: controlled status, config, and code-check commands for Hermes.
- `systemd/`: production service unit.
- `archive/`: manual tools, backtests, old execution entries, and historical data; never started by systemd.
- `data_fetcher.py`: shared HTTP session and short TTL cache to avoid duplicate BTC/ETH/Top50 requests.

Do not enable legacy cron files. Install only `systemd/trading-bot.service`.
