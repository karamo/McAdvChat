import hashlib
import json
import os
import secrets

CONFIG_PATH = "/etc/mcadvchat/config.json"
MIN_LENGTH = 8
SALT_LENGTH = 16  # results in 32 hex chars

VERSION="v0.38.0"

def load_config(path):
    with open(path, "r") as f:
        return json.load(f)

def save_config(path, config):
    with open(path, "w") as f:
        json.dump(config, f, indent=2)

def hash_magic_word(magic_word, salt=None):
    if not salt:
        salt = secrets.token_hex(SALT_LENGTH)
    salted = salt + magic_word
    hashed = hashlib.sha256(salted.encode()).hexdigest()
    return f"{salt}${hashed}"

def get_magic_word():
    while True:
        pw1 = input("Enter new magic word (min 8 chars): ").strip()
        pw2 = input("Repeat magic word: ").strip()
        if len(pw1) < MIN_LENGTH:
            print("Too short, try again.")
        elif pw1 != pw2:
            print("Magic words do not match, try again.")
        else:
            return pw1

def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"Error: Config file {CONFIG_PATH} not found.")
        return

    config = load_config(CONFIG_PATH)
    magic_word = get_magic_word()
    hashed_magic = hash_magic_word(magic_word)
    config["MAGIC_WORD_HASH"] = hashed_magic
    save_config(CONFIG_PATH, config)
    print("Magic word stored successfully.")

if __name__ == "__main__":
    main()
