"""python -m eventweather: start the server."""
import sys

from .config import Config, startup_warnings
from .forecast import ForecastService
from .server import WWW_DIR, App, make_server
from .sources import enabled_sources


def main():
    try:
        cfg = Config.from_env()
        service = ForecastService(cfg, enabled_sources(cfg))
    except ValueError as e:
        sys.exit(f"config error: {e}")
    for w in startup_warnings(cfg):
        print("warning:", w, file=sys.stderr)
    httpd = make_server(App(cfg, service, WWW_DIR), cfg.host, cfg.port)
    names = ", ".join(s.short for s in service.sources)
    print(f"Event Weather Tracker on http://{cfg.host}:{cfg.port}  (sources: {names})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
