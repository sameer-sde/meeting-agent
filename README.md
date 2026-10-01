# Meeting Agent

An AI agent that joins Teams/Google Meet meetings, transcribes every speaker in any language, and emails an English report card.

## Setup
1. python3 -m venv venv && source venv/bin/activate
2. pip install -r requirements.txt
3. Create a .env file with VEXA_API_KEY, VEXA_TX_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, REPORT_TO
4. Run Ollama with llama3.1
5. python agent.py
