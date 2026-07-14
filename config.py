"""Configuration constants for the Weather NO paper trading bot."""

import pathlib

# ── Mode ──────────────────────────────────────────────────────────────────
PAPER_TRADING = True

# ── Bankroll ──────────────────────────────────────────────────────────────
BANKROLL = 10.0
MIN_BET = 1.0
MAX_BET = 3.0
MAX_OPEN_PAPER = 10

# ── Entry Strategy ────────────────────────────────────────────────────────
DISTANCE_MIN = 2.0
DISTANCE_MAX = 4.0
NOISE_THRESHOLD = 0.5
MIN_NO_PRICE = 0.50
MAX_NO_PRICE = 0.90
MIN_VOLUME = 100
OM_MIN_DISTANCE = 1.0

# ── Monitoring ────────────────────────────────────────────────────────────
METAR_POLL_SECONDS = 45
SCAN_INTERVAL = 600
STOP_LOSS_PCT = -0.20
PROXIMITY_THRESHOLD = 10.0
METAR_CLOSE_READINGS = 2
FORECAST_DRIFT_THRESHOLD = 1.0

# ── Market Discovery ──────────────────────────────────────────────────────
LOOK_AHEAD_DAYS = 2

# ── HTTP ──────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = (5, 15)
MAX_RETRIES = 3
RETRY_DELAY = 3

# ── Paths ─────────────────────────────────────────────────────────────────
BASE_DIR = pathlib.Path(__file__).parent
STATIONS_FILE = BASE_DIR / "stations.json"
DATA_DIR = BASE_DIR / "data"
