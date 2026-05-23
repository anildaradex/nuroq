import json
import os
from datetime import datetime

class AgentMemory:
    def __init__(self, file_path="agent_memory.json"):
        self.file_path = file_path
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w") as f:
                json.dump({}, f)

    def log_decision(self, ticker, rating, score, reasoning):
        """Logs a decision to persistent memory."""
        try:
            with open(self.file_path, "r") as f:
                memory = json.load(f)
        except:
            memory = {}

        if ticker not in memory:
            memory[ticker] = []

        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "rating": rating,
            "score": score,
            "reasoning": reasoning[:200] + "..." if len(reasoning) > 200 else reasoning
        }
        
        memory[ticker].append(entry)
        # Keep only last 5 decisions per ticker
        memory[ticker] = memory[ticker][-5:]

        with open(self.file_path, "w") as f:
            json.dump(memory, f, indent=4)

    def get_past_context(self, ticker):
        """Retrieves past decisions for the AI prompt context."""
        try:
            with open(self.file_path, "r") as f:
                memory = json.load(f)
            
            if ticker not in memory or not memory[ticker]:
                return "No past history for this ticker."
            
            history = memory[ticker]
            context = "Past Decisions:\n"
            for h in history:
                context += f"- {h['timestamp']}: {h['rating']} (Score: {h['score']})\n"
            return context
        except:
            return "No past history available."
