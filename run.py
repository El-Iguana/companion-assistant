"""Dev launcher: the server with autoreload on http://127.0.0.1:7867/.

Point it at a throwaway library — ``PANTHEON_DATA_DIR=/tmp/pantheon-dev python run.py`` —
or use scripts/web-dev.sh, which refuses the real data/.
"""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.setdefault("PANTHEON_PORT", "7867"))
    uvicorn.run("service:app", host=os.environ.get("PANTHEON_BIND", "127.0.0.1"), port=port, reload=True)
