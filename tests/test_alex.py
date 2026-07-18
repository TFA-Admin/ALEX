#test_alex.py
import asyncio
import httpx

BASE_URL = "http://localhost:5000"

async def main():
    async with httpx.AsyncClient() as client:
        # 1️⃣ Introduce user and let A.L.E.X remember the name
        print("→ Sending name introduction...")
        data = {"user": "testuser", "prompt": "Hi, my name is Alice."}
        r = await client.post(f"{BASE_URL}/ask", json=data)
        print("Response:", r.json()["response"])
        
        # 2️⃣ Ask a question to confirm name memory
        print("\n→ Asking about the name...")
        data = {"user": "Alice", "prompt": "Please tell me my name."}
        r = await client.post(f"{BASE_URL}/ask", json=data)
        print("Response:", r.json()["response"])
        
        # 2026-07-18: /add_fact and /update_fact were removed from
        # api/routes.py (unauthenticated, arbitrary-key fact writes —
        # this was the only caller). A fact like favorite_color now has
        # to be set the same way a real user sets one: through the
        # conversational path itself (systems/facts/system.py), which is
        # what /ask already exercises above.

        # 3️⃣ Test memory recall
        print("\n→ Checking memory recall...")
        data = {"user": "Alice", "prompt": "Please summarize what I told you."}
        r = await client.post(f"{BASE_URL}/ask", json=data)
        print("Response:", r.json()["response"])

if __name__ == "__main__":
    asyncio.run(main())