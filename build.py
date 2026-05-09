import subprocess
import toml
import json
import tempfile
import os

pack_file = "pack.toml"
default_credits_file = (
    "./config/modpack_defaults/config/isxander-main-menu-credits.json"
)
credits_file = "./config/isxander-main-menu-credits.json"
modrinth_url = "https://modrinth.com/modpack/tritonpack"


def get_meta(pack_file: str = "pack.toml") -> dict:
    with open(pack_file, "r") as f:
        return toml.load(f)


def generate_tellraw(text: str, url: str, tooltip: str) -> dict:
    return {
        "text": text,
        "click_event": {
            "action": "open_url",
            "url": url,
        },
        "hover_event": {"action": "show_text", "value": tooltip},
    }


def update_credits(tellraw: dict, credits_file: str):
    with open(credits_file, "r") as f:
        credits = json.load(f)

    credits["main_menu"]["bottom_right"] = [tellraw]
    credits["pause_menu"]["bottom_right"] = [tellraw]

    with open(credits_file, "w") as f:
        json.dump(credits, f, indent=None)


def main():
    meta = get_meta(pack_file)
    name = meta["name"]
    version = meta["version"]
    print("Obtained modpack metadata")

    tellraw = generate_tellraw(
        text=f"{name} {version}",
        url=modrinth_url,
        tooltip=f"View on Modrinth",
    )

    update_credits(tellraw, credits_file)
    update_credits(tellraw, default_credits_file)
    print("Updated credits files")

    os.makedirs("build", exist_ok=True)

    subprocess.run(["packwiz", "refresh"], check=True)

    subprocess.run(
        [
            "packwiz",
            "mr",
            "export",
            "-o",
            f"build/{name}-{version}.mrpack",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
