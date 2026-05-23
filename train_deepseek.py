import subprocess
import sys
import os

# --- CONFIGURATION ---
# DeepSeek-R1-Distill-Qwen-7B is a strong reasoning model that fits on Mac.
MODEL_NAME = "mlx-community/DeepSeek-R1-Distill-Qwen-7B-4bit"

# Training Hyperparameters
ITERATIONS = 200
BATCH_SIZE = 1
LORA_LAYERS = 16
LEARNING_RATE = 1e-5
ADAPTER_PATH = "adapters_deepseek" # Separate folder for DeepSeek adapters

def run_training():
    print(f"🚀 Starting LoRA Fine-Tuning for: {MODEL_NAME}")
    print(f" This will download the model locally (approx 5GB) if you don't have it.")
    print(f"📊 Config: {ITERATIONS} iters | Batch {BATCH_SIZE} | LR {LEARNING_RATE}")
    
    # ensure valid.jsonl exists
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
        "--save-every", "50"
    ]

    try:
        # Run the command and stream output
        subprocess.run(command, check=True)
        print(f"\n✅ Training Complete! Adapters saved to '{ADAPTER_PATH}'.")
        print(f"To use this model in your dashboard, update dashboard.py to point to '{ADAPTER_PATH}' and the new model name.")
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Training Failed: {e}")

if __name__ == "__main__":
    run_training()
