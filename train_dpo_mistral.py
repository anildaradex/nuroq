import subprocess
import sys
import os
import random

# --- CONFIGURATION ---
MODEL_NAME = "mlx-community/Mistral-7B-Instruct-v0.3-4bit"
DPO_DATA_FILE = "dpo_train.jsonl"

# DPO Hyperparameters
ITERATIONS = 200
BATCH_SIZE = 1
LORA_LAYERS = 16
LEARNING_RATE = 5e-7 
ADAPTER_PATH = "dpo_adapters_mistral" 

def prepare_data_split():
    """Splits dpo_train.jsonl into dpo_train.jsonl and dpo_valid.jsonl if not already done."""
    print(f"📂 Preparing data split for Mistral DPO...")
    
    if not os.path.exists(DPO_DATA_FILE):
        print(f"❌ Error: {DPO_DATA_FILE} not found. Run 'python create_dpo_training_json.py' first.")
        sys.exit(1)

    with open(DPO_DATA_FILE, "r") as f:
        lines = f.readlines()
    
    random.shuffle(lines)
    split = int(len(lines) * 0.9)
    train_lines = lines[:split]
    valid_lines = lines[split:]

    os.makedirs("dpo_data_mistral", exist_ok=True)
    with open("dpo_data_mistral/train.jsonl", "w") as f:
        f.writelines(train_lines)
    with open("dpo_data_mistral/valid.jsonl", "w") as f:
        f.writelines(valid_lines)
    
    print(f"✅ Created 'dpo_data_mistral/' with {len(train_lines)} train and {len(valid_lines)} samples.")

def run_dpo_training():
    prepare_data_split()

    print(f"🚀 Starting DPO for Mistral: {MODEL_NAME}")
    
    command = [
        sys.executable, "-m", "mlx_lm.lora",
        "--model", MODEL_NAME,
        "--train",
        "--data", "dpo_data_mistral",
        "--iters", str(ITERATIONS),
        "--batch-size", str(BATCH_SIZE),
        "--num-layers", str(LORA_LAYERS),
        "--learning-rate", str(LEARNING_RATE),
        "--adapter-path", ADAPTER_PATH,
        "--save-every", "50"
    ]

    try:
        subprocess.run(command, check=True)
        print(f"\n✅ Mistral DPO Complete! Adapters saved to '{ADAPTER_PATH}'.")
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Mistral DPO Failed: {e}")

if __name__ == "__main__":
    run_dpo_training()