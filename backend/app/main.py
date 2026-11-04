"""
TradeLens AI - Backend REST API Service
Serves endpoints for live shortlisting, deep-dive stock analysis, and position monitoring.
Runs natively using Python's http.server (zero dependencies) and is fully FastAPI-compatible.
"""
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Ensure core packages can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.sample_universe import SampleUniverseProvider
from app.core.shortlist_engine import (
    scan_and_shortlist_universe,
    evaluate_single_stock_full_profile,
    monitor_open_positions
)

provider = SampleUniverseProvider()


class TradeLensAPIHandler(BaseHTTPRequestHandler):
    def _set_cors_headers(self, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_cors_headers(200)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/health" or path == "/":
            self._set_cors_headers(200)
            res = {"status": "ok", "service": "TradeLens AI Market Engine", "version": "1.0.0"}
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif path == "/api/scan":
            self._set_cors_headers(200)
            sentiment = query.get("sentiment", ["BULLISH"])[0]
            top_n = int(query.get("top_n", [5])[0])
            all_stocks = provider.get_all_stocks()
            result = scan_and_shortlist_universe(all_stocks, top_n=top_n, market_sentiment=sentiment)
            result["indices"] = provider.get_indices()
            self.wfile.write(json.dumps(result, indent=2).encode("utf-8"))

        elif path == "/api/indices":
            self._set_cors_headers(200)
            self.wfile.write(json.dumps({"indices": provider.get_indices()}, indent=2).encode("utf-8"))

        elif path.startswith("/api/stock/"):
            symbol = path.split("/api/stock/")[1].strip().upper()
            all_stocks = provider.get_all_stocks()
            target = next((s for s in all_stocks if s["symbol"] == symbol), None)
            
            if target:
                self._set_cors_headers(200)
                profile = evaluate_single_stock_full_profile(target)
                self.wfile.write(json.dumps(profile, indent=2).encode("utf-8"))
            else:
                self._set_cors_headers(404)
                self.wfile.write(json.dumps({"error": f"Symbol '{symbol}' not found in universe"}).encode("utf-8"))

        elif path == "/api/universe":
            self._set_cors_headers(200)
            all_stocks = provider.get_all_stocks()
            summary = [
                {
                    "symbol": s["symbol"],
                    "name": s["name"],
                    "category": s.get("category", "LargeCap"),
                    "cmp": s["candles"][-1]["close"],
                    "lot_size": s.get("lot_size", 250)
                }
                for s in all_stocks
            ]
            self.wfile.write(json.dumps({"total": len(summary), "stocks": summary}).encode("utf-8"))

        else:
            self._set_cors_headers(404)
            self.wfile.write(json.dumps({"error": "Endpoint not found"}).encode("utf-8"))

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/positions/monitor":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8"))
                positions = data.get("positions", [])
                all_stocks = provider.get_all_stocks()
                profiles = {s["symbol"]: evaluate_single_stock_full_profile(s) for s in all_stocks}
                alerts = monitor_open_positions(positions, profiles)
                self._set_cors_headers(200)
                self.wfile.write(json.dumps({"alerts": alerts}, indent=2).encode("utf-8"))
            except Exception as e:
                self._set_cors_headers(400)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        else:
            self._set_cors_headers(404)
            self.wfile.write(json.dumps({"error": "Endpoint not found"}).encode("utf-8"))


def run_server(port=8000):
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, TradeLensAPIHandler)
    print(f"🚀 TradeLens AI Backend API running at http://127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    run_server(port)

