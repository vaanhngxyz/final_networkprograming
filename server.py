#!/usr/bin/env python3
import socket
import threading
import queue
import select
import json
import random
import string
import time
from datetime import datetime

# --- Cấu hình chung giống server.c ---
PORT = 12345
MAX_PLAYERS = 5
TIMEOUT_SEC = 30
TIMEOUT_PENALTY = -2
WIN_SCORE = 50
JSON_FILE = "game_log.json"
BONUS_TIME = 5
BONUS_POINTS = 5
DICT_FILE = "dictionary.txt"

lock = threading.Lock()
players = []            # list of dicts: {'socket','name','is_host','score','ready'}
player_queues = {}      # idx -> Queue of JSON word submissions
used_words = []
current_letter = None
game_active = False
current_cycle = 1
dict_words = set()

def load_dictionary(path=DICT_FILE):
    with open(path, encoding='utf-8') as f:
        for line in f:
            w = line.strip().lower()
            if w:
                dict_words.add(w)
    print(f"Loaded {len(dict_words)} words into dictionary.")

def log_play_state(player, word, state, score_change, current_score):
    global current_cycle
    entry = {
        "Cycle": str(current_cycle),
        "player": player,
        "word": word,
        "server_timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "state": state,
        "score_change": score_change,
        "current_score": current_score
    }
    with open(JSON_FILE, "a") as f:
        json.dump(entry, f)
        f.write("\n")

def broadcast(msg):
    """Gửi msg (string) cho tất cả client, tự thêm '\n'."""
    data = (msg + "\n").encode()
    with lock:
        for p in players:
            try:
                p['socket'].sendall(data)
            except:
                pass

def broadcast_chat(name, text):
    full = f"CHAT [{name}]: {text}\n".encode()
    with lock:
        for p in players:
            try:
                p['socket'].sendall(full)
            except:
                pass

def broadcast_scores():
    with lock:
        parts = [f"{p['name']}:{p['score']}" for p in players]
    broadcast("SCORES " + ",".join(parts))

def validate_word(word, expected):
    wl = word.lower()
    if not wl or wl[0] != expected.lower(): return False
    if wl not in dict_words: return False
    if wl in used_words: return False
    return True

def handle_client(sock, idx):
    """Thread xử lý REGISTER, START, CHAT, và nhận JSON từ client."""
    global game_active
    q = queue.Queue()
    player_queues[idx] = q

    # --- Đăng ký ---
    try:
        while True:
            data = sock.recv(1024)
            if not data:
                raise ConnectionError()
            for line in data.decode().splitlines():
                if line.startswith("REGISTER "):
                    name = line[9:].strip()
                    with lock:
                        players[idx].update({
                            'name': name,
                            'ready': True,
                            'score': 0,
                        })
                        players[idx]['is_host'] = (idx == 0)
                    broadcast(f"INFO Player {name} joined the game")
                    if players[idx]['is_host']:
                        sock.sendall(b"INFO You are the host.\n")
                    break
            else:
                continue
            break

        # --- Sau khi đăng ký, loop nhận lệnh START, CHAT, JSON từ client ---
        while True:
            data = sock.recv(1024)
            if not data:
                raise ConnectionError()
            for line in data.decode().splitlines():
                if line.strip() == "START":
                    with lock:
                        if not players[idx]['is_host']:
                            sock.sendall(b"ERROR: Only the host can start the game.\n")
                        elif game_active:
                            sock.sendall(b"ERROR: Game already in progress.\n")
                        else:
                            ready_cnt = sum(1 for p in players if p['ready'])
                            if ready_cnt < 2:
                                sock.sendall(b"ERROR: Need at least 2 players.\n")
                            else:
                                game_active = True
                                threading.Thread(target=game_loop, daemon=True).start()
                elif line.startswith("CHAT "):
                    broadcast_chat(players[idx]['name'], line[5:])
                elif line.startswith("{"):
                    # JSON chứa từ chơi
                    try:
                        msg = json.loads(line)
                        q.put(msg)
                    except:
                        pass
                # else: bỏ qua
    except:
        # Xử lý ngắt kết nối
        with lock:
            pname = players[idx].get('name', '')
            broadcast(f"INFO Player {pname} disconnected")
            del players[idx]
            del player_queues[idx]
        broadcast_scores()
        sock.close()

def game_loop():
    """Luồng chính cho vòng chơi."""
    global current_letter, current_cycle, game_active
    with lock:
        # reset state
        current_cycle = 1
        turns_in_cycle = 0
        used_words.clear()
    current_letter = random.choice(string.ascii_lowercase)
    broadcast(f"INFO Game starting! First letter: {current_letter}")

    turn = 0
    while True:
        with lock:
            if turn >= len(players):
                break
            p = players[turn]
        sock = p['socket']
        name = p['name']
        sock.sendall(f"PROMPT {current_letter}\n".encode())

        start_t = time.time()
        state = "invalid"
        score_change = 0
        word = ""

        # chờ JSON từ queue
        try:
            msg = player_queues[turn].get(timeout=TIMEOUT_SEC)
            word = msg.get("word", "")
            early = (time.time() - start_t) <= BONUS_TIME
            duplicate = used_words and used_words[-1].lower() == word.lower()

            if validate_word(word, current_letter) and not duplicate:
                used_words.append(word.lower())
                score_change = len(word) + (BONUS_POINTS if early else 0)
                state = "bonus" if early else "accept"
                p['score'] += score_change
                current_letter = word[-1].lower()
            else:
                score_change = -1
                state = "invalid"
                p['score'] += score_change
        except queue.Empty:
            # timeout
            state = "timeout"
            score_change = TIMEOUT_PENALTY
            p['score'] += score_change

        # gửi JSON phản hồi
        resp = {
            "Cycle": str(current_cycle),
            "player": name,
            "word": word,
            "server_timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "state": state,
            "score_change": score_change,
            "current_score": p['score']
        }
        sock.sendall((json.dumps(resp) + "\n").encode())
        log_play_state(name, word, state, score_change, p['score'])

        # broadcast thông tin và điểm
        if state in ("accept", "bonus"):
            broadcast(f"INFO {name} played '{word}' (+{score_change} points)")
        elif state == "timeout":
            broadcast(f"INFO {name} ran out of time ({score_change} points)")
        else:
            if state == "invalid":
                if used_words and used_words[-1].lower() == word.lower():
                    broadcast(f"INFO Word '{word}' was already used")
                else:
                    broadcast(f"INFO Word '{word}' is not valid")
        broadcast_scores()

        # kiểm tra chiến thắng
        if p['score'] >= WIN_SCORE:
            with lock:
                parts = [f"{p['name']}: {p['score']}"]
                for o in players:
                    if o is not p:
                        parts.append(f"{o['name']}: {o['score']}")
            broadcast("ENDGAME " + ",".join(parts))
            break

    #Cập nhật lượt và chu kỳ
        turns_in_cycle += 1
        if turns_in_cycle == len(players):
            current_cycle += 1
            turns_in_cycle = 0

        # next turn
        with lock:
            
            turn = (turn + 1) % len(players)

    game_active = False

def accept_loop():
    load_dictionary()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('', PORT))
    srv.listen(MAX_PLAYERS)
    print(f"Server listening on port {PORT}...")

    while True:
        cli, addr = srv.accept()
        with lock:
            if len(players) >= MAX_PLAYERS:
                cli.sendall(b"INFO Server full.\n")
                cli.close()
                continue
            idx = len(players)
            players.append({
                'socket': cli,
                'name': "",
                'is_host': False,
                'score': 0,
                'ready': False
            })
        threading.Thread(target=handle_client, args=(cli, idx), daemon=True).start()

if __name__ == "__main__":
    accept_loop()
