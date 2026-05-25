from pathlib import Path
from urllib.request import urlretrieve


MODEL_URL = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt"
MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "sam2.1_hiera_tiny.pt"


def main():
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 100_000_000:
        print(f"SAM2 checkpoint already present: {MODEL_PATH}")
        return
    print(f"Downloading SAM2 checkpoint to {MODEL_PATH}")
    urlretrieve(MODEL_URL, MODEL_PATH)
    print("Done")


if __name__ == "__main__":
    main()
