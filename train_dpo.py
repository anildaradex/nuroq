"""
train_dpo.py — Gemma DPO via mlx-lm-lora on Apple Silicon.

Reads dpo_data/{train,valid}.jsonl produced by create_dpo_training_json.py and
fine-tunes a LoRA adapter on Gemma using DPO. Output adapter goes to
adapters/ by default — matching the path dashboard.py expects to load.
"""

import argparse
import os
import subprocess
import sys


DEFAULT_MODEL    = "mlx-community/gemma-3-4b-it-4bit"
DEFAULT_DATA_DIR = "dpo_data"
DEFAULT_ADAPTER  = "adapters_gemma_dpo"


def run_dpo(args):
    if not os.path.isdir(args.data):
        sys.exit(f"❌ {args.data}/ not found — run `python create_dpo_training_json.py` first.")
    if not os.path.exists(os.path.join(args.data, "train.jsonl")):
        sys.exit(f"❌ {args.data}/train.jsonl missing.")

    cmd = [
        sys.executable, "-m", "mlx_lm_lora.train",
        "--model", args.model,
        "--train",
        "--train-mode", "dpo",
        "--data", args.data,
        "--beta", str(args.beta),
        "--dpo-cpo-loss-type", args.loss_type,
        "--iters", str(args.iters),
        "--batch-size", str(args.batch_size),
        "--num-layers", str(args.num_layers),
        "--learning-rate", str(args.learning_rate),
        "--adapter-path", args.adapter_path,
        "--save-every", str(args.save_every),
    ]
    if args.reference_model:
        cmd += ["--reference-model-path", args.reference_model]

    print(f"🚀 Starting Gemma DPO\n   model:   {args.model}\n   data:    {args.data}\n"
          f"   adapter: {args.adapter_path}\n   iters:   {args.iters}\n")
    print("   $", " ".join(cmd), "\n")

    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        sys.exit("❌ mlx_lm_lora not installed. Run: uv sync   (mlx-lm-lora must be in pyproject.toml)")
    except subprocess.CalledProcessError as e:
        sys.exit(f"❌ DPO training failed: {e.returncode}")

    print(f"\n✅ DPO complete. Adapter saved to '{args.adapter_path}/'.")
    print("   Wire it into dashboard.py by setting MODELS_CFG['gemma']['adapter'] = "
          f"'{args.adapter_path}'.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Base model (default: {DEFAULT_MODEL})")
    p.add_argument("--data", default=DEFAULT_DATA_DIR,
                   help="Dir containing train.jsonl + valid.jsonl")
    p.add_argument("--adapter-path", default=DEFAULT_ADAPTER,
                   help="Where to save the LoRA adapter")
    p.add_argument("--iters", type=int, default=400)
    p.add_argument("--batch-size", type=int, default=2)
    p.add_argument("--num-layers", type=int, default=16)
    p.add_argument("--learning-rate", type=float, default=5e-7)
    p.add_argument("--beta", type=float, default=0.1,
                   help="DPO KL penalty (lower = closer to reference model)")
    p.add_argument("--loss-type", default="sigmoid",
                   choices=["sigmoid", "hinge", "ipo", "dpop"])
    p.add_argument("--reference-model", default=None,
                   help="Reference model path (defaults to --model if unset)")
    p.add_argument("--save-every", type=int, default=100)
    run_dpo(p.parse_args())


if __name__ == "__main__":
    main()
