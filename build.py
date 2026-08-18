import subprocess
import toml
import json
import os

pack_file = "pack.toml"

default_credits_file = (
    "./config/modpack_defaults/config/isxander-main-menu-credits.json"
)
credits_file = "./config/isxander-main-menu-credits.json"

default_update_file = (
    "./config/modpack_defaults/config/simple-modpack-update-checker.json"
)
update_file = "./config/simple-modpack-update-checker.json"


modrinth_url = "https://modrinth.com/modpack/tritonpack"
modrinth_slug = "CP4fTcs0"

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

    credits["main_menu"]["bottom_left"] = [tellraw]
    credits["main_menu"]["bottom_right"] = []
    credits["pause_menu"]["bottom_right"] = [tellraw]
    credits["pause_menu"]["bottom_left"] = []

    with open(credits_file, "w") as f:
        json.dump(credits, f, indent=None)


def update_update_checker(version: str, pack_ver: str, update_file: str):
    with open(update_file, "r") as f:
        update_checker = json.load(f)

    update_checker["localVersion"] = version
    update_checker["identifier"] = modrinth_slug
    update_checker["minecraftVersions"] = [pack_ver]

    with open(update_file, "w") as f:
        json.dump(update_checker, f, indent=None)


def main():
    meta = get_meta(pack_file)
    name = meta["name"]
    version = meta["version"]
    game_version = meta["versions"]["minecraft"]
    print("Obtained modpack metadata")

    tellraw = generate_tellraw(
        text=f"{name} {version}",
        url=modrinth_url,
        tooltip=f"View on Modrinth",
    )

    update_credits(tellraw, credits_file)
    update_credits(tellraw, default_credits_file)
    print("Updated credits files")

    update_update_checker(version, game_version, update_file)
    update_update_checker(version, game_version, default_update_file)
    print("Updated update checker files")

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
