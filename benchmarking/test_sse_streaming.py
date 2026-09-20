# benchmarking/test_sse_streaming.py
import sys
import os
import json
import time
import requests
import uuid

BASE_DIR = "/home/techyz-admin/sevenwings/03_learning/26-08-10-ai-system/multi-tenant-rag-system"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.database import SessionLocal
from modules.auth.domain.models import User, Organization
from modules.auth.services.token_service import create_access_token

BASE_URL = "http://127.0.0.1:4500"

def get_auth_token():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "iarowosola@yahoo.com").first()
        if not user:
            user = db.query(User).first()
        assert user is not None, "Test user must exist"
        
        token = create_access_token(
            data={
                "sub": user.id,
                "email": user.email,
                "org_id": user.org_id,
                "role": user.role.value if hasattr(user.role, "value") else str(user.role),
                "dept_id": user.department_id
            }
        )
        return token
    finally:
        db.close()

def test_sse_stream_query(query: str, session_id: str, token: str):
    print("\n" + "=" * 80)
    print(f"TESTING SSE STREAM FOR QUERY: '{query}'")
    print(f"Session ID: {session_id}")
    print("=" * 80)

    url = f"{BASE_URL}/chat/query"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream"
    }
    payload = {
        "query": query,
        "session_id": session_id,
        "stream": True
    }

    start_time = time.time()
    resp = requests.post(url, json=payload, headers=headers, stream=True, timeout=180)
    assert resp.status_code == 200, f"Expected 200 OK, got {resp.status_code}: {resp.text}"
    assert "text/event-stream" in resp.headers.get("content-type", ""), f"Expected text/event-stream, got {resp.headers.get('content-type')}"

    status_events = []
    delta_tokens = []
    metadata_event = None

    current_event = None
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            current_event = None
            continue
        if line.startswith("event:"):
            current_event = line.replace("event:", "").strip()
        elif line.startswith("data:"):
            data_str = line.replace("data:", "").strip()
            data = json.loads(data_str)
            if current_event == "status":
                status_events.append(data)
                status_icon = "✓" if data.get("status") == "completed" else "..."
                print(f"  [STATUS] [{data.get('stage', 'pipeline').upper()}] {status_icon} {data.get('title')}: {data.get('details')}")
            elif current_event == "delta":
                token_text = data.get("content", "")
                delta_tokens.append(token_text)
                sys.stdout.write(token_text)
                sys.stdout.flush()
            elif current_event == "metadata":
                metadata_event = data
                print(f"\n  [METADATA] Latency: {data.get('latency_ms')}ms | Sources: {len(data.get('sources', []))} | Grounded: {data.get('is_grounded')}")

    total_time = time.time() - start_time
    full_answer = "".join(delta_tokens)

    print(f"\nStream Completed in {total_time:.2f}s")
    print(f"Status Events Received: {len(status_events)}")
    print(f"Tokens Streamed: {len(delta_tokens)} (Length: {len(full_answer)} chars)")
    print(f"Metadata Event Received: {metadata_event is not None}")

    # Assertions
    assert len(status_events) >= 2, f"Expected at least 2 status milestones, got {len(status_events)}"
    step_names = [s.get("step") for s in status_events]
    assert "context_resolution" in step_names, f"context_resolution milestone missing from {step_names}"
    assert "query_planning" in step_names, f"query_planning milestone missing from {step_names}"
    assert len(full_answer.strip()) > 0, "Expected non-empty streamed answer"
    assert metadata_event is not None, "Expected final metadata event"
    assert "sources" in metadata_event, "Metadata event must contain sources"
    assert "latency_ms" in metadata_event, "Metadata event must contain latency_ms"

    print("✓ SSE Streaming Validation PASSED!")
    return full_answer, status_events, metadata_event

def test_sync_fallback(query: str, session_id: str, token: str):
    print("\n" + "=" * 80)
    print(f"TESTING SYNCHRONOUS BACKWARD COMPATIBILITY (stream=False): '{query}'")
    print("=" * 80)

    url = f"{BASE_URL}/chat/query"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "query": query,
        "session_id": session_id,
        "stream": False
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=180)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert "application/json" in resp.headers.get("content-type", ""), f"Expected JSON, got {resp.headers.get('content-type')}"

    data = resp.json()
    assert data.get("status") == "success", f"Status not success: {data}"
    assert "answer" in data and len(data["answer"].strip()) > 0, "Empty answer"
    assert "sources" in data, "Sources missing"
    print(f"Answer: {data['answer'][:150]}...")
    print(f"Sources: {len(data.get('sources', []))}")
    print("✓ Synchronous Fallback Validation PASSED!")

if __name__ == "__main__":
    token = get_auth_token()
    session_id = f"test-sse-{uuid.uuid4().hex[:8]}"

    # 1. Test Dynamic SQL Streaming Route
    test_sse_stream_query("Who is the customer associated with BVN 90000001008?", session_id, token)

    # 2. Test Conversational Streaming Route
    test_sse_stream_query("Hello, can you introduce yourself?", session_id, token)

    # 3. Test Synchronous Backward Compatibility
    test_sync_fallback("Hello, what is your role and how can you help me?", session_id, token)

    print("\n" + "=" * 80)
    print("ALL SSE STREAMING AND DUAL-MODE TESTS PASSED SUCCESSFULLY!")
    print("=" * 80)
