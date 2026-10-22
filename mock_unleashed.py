"""Run the mock Unleashed API as its own server (default http://localhost:8100).

Point the dashboard at it with:
    UNLEASHED_MODE=live UNLEASHED_BASE_URL=http://localhost:8100 \
    UNLEASHED_API_ID=mock-api-id UNLEASHED_API_KEY=mock-api-key python run.py
"""
import os

from unleashed.mock_server import MOCK_API_ID, MOCK_API_KEY, create_mock_app

app = create_mock_app(os.environ.get("MOCK_API_ID", MOCK_API_ID),
                      os.environ.get("MOCK_API_KEY", MOCK_API_KEY))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("MOCK_PORT", "8100")))
