import subprocess
import sys
import os
import json
import random

# --- CONFIGURATION ---
MODEL_NAME = "mlx-community/DeepSeek-R1-Distill-Qwen-7B-4bit"
DPO_DATA_FILE = "dpo_train.jsonl"

# DPO Hyperparameters (DPO is more sensitive than SFT)
ITERATIONS = 200
BATCH_SIZE = 1
LORA_LAYERS = 16
LEARNING_RATE = 5e-7 # DPO usually requires a smaller LR
ADAPTER_PATH = "dpo_adapters_deepseek" 

def prepare_data_split():
    """Splits dpo_train.jsonl into dpo_train.jsonl and dpo_valid.jsonl if not already done."""
    print(f"📂 Preparing data split for DPO...")
    
    if not os.path.exists(DPO_DATA_FILE):
        print(f"❌ Error: {DPO_DATA_FILE} not found. Run 'python create_dpo_training_json.py' first.")
        sys.exit(1)

    with open(DPO_DATA_FILE, "r") as f:
        lines = f.readlines()
    
    if len(lines) < 2:
        print("❌ Error: Not enough samples for training.")
        sys.exit(1)

    random.shuffle(lines)
    split = int(len(lines) * 0.9)
    train_lines = lines[:split]
    valid_lines = lines[split:]

    # mlx_lm.dpo expects data in a folder with 'train.jsonl' and 'valid.jsonl'
    # We will create a temp folder 'dpo_data'
    os.makedirs("dpo_data", exist_ok=True)
    with open("dpo_data/train.jsonl", "w") as f:
        f.writelines(train_lines)
    with open("dpo_data/valid.jsonl", "w") as f:
        f.writelines(valid_lines)
    
    print(f"✅ Created 'dpo_data/' with {len(train_lines)} train and {len(valid_lines)} valid samples.")

def run_dpo_training():
    prepare_data_split()

    print(f"🚀 Starting DPO (Direct Preference Optimization) for: {MODEL_NAME}")
    print(f"📊 Config: {ITERATIONS} iters | LR {LEARNING_RATE} | Adapters: {ADAPTER_PATH}")
    
    # Construct the training command
    command = [
        sys.executable, "-m", "mlx_lm.lora",
        "--model", MODEL_NAME,
        "--train",
        "--data", "dpo_data",
        "--iters", str(ITERATIONS),
        "--batch-size", str(BATCH_SIZE),
        "--num-layers", str(LORA_LAYERS),
        "--learning-rate", str(LEARNING_RATE),
        "--adapter-path", ADAPTER_PATH,
        "--save-every", "50"
    ]

    try:
        subprocess.run(command, check=True)
        print(f"\n✅ DPO Training Complete! Adapters saved to '{ADAPTER_PATH}'.")
        print(f"To use: Update dashboard.py to use the adapter_path='{ADAPTER_PATH}'")
    except subprocess.CalledProcessError as e:
        print(f"\n❌ DPO Training Failed: {e}")

if __name__ == "__main__":
    run_dpo_training()