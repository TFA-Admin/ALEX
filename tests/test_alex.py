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
        
        # 3️⃣ Add a personal fact (favorite color)
        print("\n→ Adding a personal fact...")
        data = {"user": "Alice", "key": "favorite_color", "value": "blue"}
        r = await client.post(f"{BASE_URL}/add_fact", json=data)
        print("Add fact response:", r.json())
        
        # 4️⃣ Ask a question about the fact
        print("\n→ Asking about favorite color...")
        data = {"user": "Alice", "prompt": "What is my favorite color?"}
        r = await client.post(f"{BASE_URL}/ask", json=data)
        print("Response:", r.json()["response"])
        
        # 5️⃣ Update the fact and ask again
        print("\n→ Updating favorite color...")
        data = {"user": "Alice", "key": "favorite_color", "value": "green"}
        r = await client.post(f"{BASE_URL}/update_fact", json=data)
        print("Update fact response:", r.json())
        
        print("\n→ Asking updated favorite color...")
        data = {"user": "Alice", "prompt": "What is my favorite color now?"}
        r = await client.post(f"{BASE_URL}/ask", json=data)
        print("Response:", r.json()["response"])
        
        # 6️⃣ Test memory recall
        print("\n→ Checking memory recall...")
        data = {"user": "Alice", "prompt": "Please summarize what I told you."}
        r = await client.post(f"{BASE_URL}/ask", json=data)
        print("Response:", r.json()["response"])

if __name__ == "__main__":
    asyncio.run(main())