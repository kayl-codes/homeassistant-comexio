import argparse
import json
import os


def find_manifest_file():
    for root, _, files in os.walk(os.getcwd()):
        if "manifest.json" in files:
            return os.path.join(root, "manifest.json")
    raise FileNotFoundError("manifest.json file not found in the project directory.")


def update_manifest_version(version):
    try:
        manifest_path = find_manifest_file()
        print(f"Found manifest.json at: {manifest_path}")
        with open(manifest_path) as file:
            manifest_data = json.load(file)
        manifest_data["version"] = version
        with open(manifest_path, "w") as file:
            json.dump(manifest_data, file, indent=2)
        print(f"Successfully updated version to {version} in manifest.json.")
    except FileNotFoundError as e:
        print(e)
    except Exception as e:
        print(f"An error occurred: {e}")


def main():
    parser = argparse.ArgumentParser(description="Update the version in manifest.json.")
    parser.add_argument("--version", required=True, help="New version to set.")
    args = parser.parse_args()
    update_manifest_version(args.version)


if __name__ == "__main__":
    main()
