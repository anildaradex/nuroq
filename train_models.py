import subprocess
import sys
import os
import time

def run_script(script_name):
    """Executes a python script and handles errors."""
    print(f"\n" + "="*50)
    print(f"🚀 EXECUTING: {script_name}")
    print("="*50 + "\n")
    
    try:
        # Use sys.executable to ensure we use the same environment
        subprocess.run([sys.executable, script_name], check=True)
        print(f"\n✅ SUCCESS: {script_name} completed.")
    except subprocess.CalledProcessError as e:
        print(f"\n❌ FAILED: {script_name} exited with error: {e}")
        sys.exit(1)

def main():
    start_time = time.time()
    
    print("📈 --- STOCK AI MASTER TRAINING PIPELINE --- 📈")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 1. Generate SFT Data (Reasoning Chains)
    # This creates the prompt/completion pairs for training
    run_script("create_training_json.py")
    
    # 2. Train DeepSeek-R1 (SFT)
    run_script("train_deepseek.py")
    
    # 3. Train Mistral-7B (SFT)
    run_script("train_mistral.py")
    
    end_time = time.time()
    duration = (end_time - start_time) / 60
    
    print("\n" + "!"*50)
    print("🏁 ALL TRAINING TASKS COMPLETE!")
    print(f"⏱️ Total Pipeline Duration: {duration:.2f} minutes")
    print(f"📂 Adapters saved to 'adapters_deepseek' and 'adapters'")
    print("!"*50)
    print("\nYou can now run your dashboard in Ensemble mode:")
    print("uv run python dashboard.py --mode ensemble")

if __name__ == "__main__":
    main()