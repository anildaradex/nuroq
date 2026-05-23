import subprocess
import sys
import os

# --- CONFIGURATION ---
# Options: 
# 1. "mlx-community/Mistral-7B-Instruct-v0.3-4bit" (Your current standard)
# 2. "mlx-community/DeepSeek-Coder-V2-Lite-Instruct-4bit-mlx" (Good coding/logic model)
# 3. "mlx-community/DeepSeek-LLM-7B-Chat-4bit-mlx" (General purpose DeepSeek)

# Let's default to Mistral, but you can change this easily
MODEL_NAME = "mlx-community/Mistral-7B-Instruct-v0.3-4bit"

# Training Hyperparameters
ITERATIONS = 200
BATCH_SIZE = 1
LORA_LAYERS = 16
LEARNING_RATE = 1e-5
ADAPTER_PATH = "adapters"

def run_training():
    print(f"🚀 Starting LoRA Fine-Tuning for: {MODEL_NAME}")
    print(f"📊 Config: {ITERATIONS} iters | Batch {BATCH_SIZE} | LR {LEARNING_RATE}")
    
    # ensure valid.jsonl exists, if not copy train.jsonl (simple hack for valid)
    if not os.path.exists("valid.jsonl"):
        print("⚠️ valid.jsonl not found. Creating a small validation set from train.jsonl...")
        os.system("head -n 50 train.jsonl > valid.jsonl")

    # Construct the command
    command = [
        sys.executable, "-m", "mlx_lm.lora",
        "--model", MODEL_NAME,
        "--train",
        "--data", ".",  # Looks for train.jsonl and valid.jsonl in current dir
        "--iters", str(ITERATIONS),
        "--batch-size", str(BATCH_SIZE),
        "--num-layers", str(LORA_LAYERS),
        "--learning-rate", str(LEARNING_RATE),
        "--adapter-path", ADAPTER_PATH,
        "--save-every", "100"
    ]

    try:
        # Run the command and stream output
        subprocess.run(command, check=True)
        print(f"\n✅ Training Complete! Adapters saved to '{ADAPTER_PATH}'.")
        print(f"To test: python dashboard.py")
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Training Failed: {e}")

if __name__ == "__main__":
    # Optional: Allow passing model name as argument
    if len(sys.argv) > 1:
        MODEL_NAME = sys.argv[1]
    
    run_training()
