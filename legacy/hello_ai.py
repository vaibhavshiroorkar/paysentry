import os
from openai import OpenAI

client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=os.environ.get("GROQ_API_KEY")
)

try:
    print(" ⚡ Waking up Groq (Round 3)...")
    
    response = client.chat.completions.create(
        # UPDATED MODEL NAME BELOW
        model="llama-3.3-70b-versatile", 
        messages=[
            {"role": "system", "content": "You are a witty Fintech teacher."},
            {"role": "user", "content": "Explain what a 'Payment Gateway' is in one sentence."}
        ]
    )
    
    answer = response.choices[0].message.content
    print(f"\nAI Answer: {answer}")

except Exception as e:
    print(f"❌ Error: {e}")