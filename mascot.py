import time
import threading
from rich.live import Live
from rich.text import Text
from rich.console import Group
from rich.panel import Panel

# ANSI Color Codes
COLORS = {
    '{B}': '\033[94m', # Light Blue (Base Body)
    '{Y}': '\033[93m', # Yellow (Horns, Stars)
    '{W}': '\033[97m', # White (Eye frame)
    '{C}': '\033[96m', # Cyan (Magic/Pupil)
    '{R}': '\033[91m', # Red (Base Mouth)
    '{G}': '\033[92m', # Green (Bronze Armor)
    '{M}': '\033[95m', # Magenta (Wizard Robes/Hat)
    '{X}': '\033[0m'   # Reset
}

# 28-Character Strict Alignment Grid
TEMPLATES = {
    1: [
        "                            ",
        "                            ",
        "        {Y}/\\{B}        {Y}/\\{B}        ",
        "       {Y}/{B}  \\______/  {Y}\\{B}       ",
        "      |   {W}.------.{B}   |      ",
        "      |  {W}|{B}{EYE}{W}|{B}  |      ",
        "      |   {W}'------'{B}   |      ",
        "      |  {R}.--------.{B}  |      ",
        "     /|  {R}\\________/{B}  |\\     ",
        "    |_|              |_|    ",
        "      |______________|      ",
        "        | |      | |        ",
        "        |_|      |_|        "
    ],
    2: [
        "                            ",
        "                            ",
        "        {Y}/\\{B}  {G}____{B}  {Y}/\\{B}        ",
        "       {Y}/{B}  \\{G}======{B}/  {Y}\\{B}       ",
        "      |   {W}.------.{B}   |      ",
        "      |  {W}|{B}{EYE}{W}|{B}  |      ",
        "      |   {W}'------'{B}   |      ",
        "      | {G}[==========]{B} |      ",
        "     /| {G}[==========]{B} |\\     ",
        "    {G}[_]{B} {G}[==========]{B} {G}[_]{B}    ",
        "      |______________|      ",
        "        {G}| |{B}      {G}| |{B}        ",
        "        {G}[_]{B}      {G}[_]{B}        "
    ],
    3: [
        "                            ",
        "            {M}____{B}            ",
        "        {Y}/\\{B} {M}/_{Y}**{M}_\\{B} {Y}/\\{B}        ",
        "  {C}*{M}O{C}*{B}  {Y}/{B}  \\{M}/____\\{B}/  {Y}\\{B}       ",
        "   {M}|{B}  |   {M}[------]{B}   |      ",
        "  {M}-+-{B} |  {M}|{B}{EYE}{M}|{B}  |      ",
        "   {M}|{B}  |   {M}[------]{B}   |      ",
        "   {M}|{B}  | {M}~::::::::::~{B} |      ",
        "   {M}|{B} /| {M}~::::::::::~{B} |\\     ",
        "   {M}|{B}{M}[_]{B} {M}~::::::::::~{B} {M}[_]{B}    ",
        "  {M}/ \\{B} |______________|      ",
        "    {C}~{B}   {M}| |{B}      {M}| |{B}   {C}~{B}    ",
        "        {M}[_]{B}      {M}[_]{B}        "
    ]
}

# The eye blocks MUST always be exactly 8 visible characters wide
EYES = {
    1: {"idle": "  {C}[+]{B}   ", "look_left": " {C}[+]{B}    ", "look_right": "    {C}[+]{B} ", "blink": " {C}>----<{B} "},
    2: {"idle": "  {C}[+]{B}   ", "look_left": " {C}[+]{B}    ", "look_right": "    {C}[+]{B} ", "blink": " {C}>----<{B} "},
    3: {"idle": "  {C}[*]{B}   ", "look_left": " {C}[*]{B}    ", "look_right": "    {C}[*]{B} ", "blink": " {M}>----<{B} "}
}

EVOLUTION_STATES = {}

def pre_process_animations():
    for lvl in [1, 2, 3]:
        EVOLUTION_STATES[lvl] = {}
        for anim in ["idle", "look_left", "look_right", "blink"]:
            frame_lines = []
            for line in TEMPLATES[lvl]:
                # Insert the eyes, then apply the base color wrapper
                formatted_line = "{B}" + line.replace("{EYE}", EYES[lvl][anim]) + "{X}"
                # Translate color tags to ANSI codes
                for tag, code in COLORS.items():
                    formatted_line = formatted_line.replace(tag, code)
                frame_lines.append(formatted_line)
            EVOLUTION_STATES[lvl][anim] = "\n".join(frame_lines)

pre_process_animations()

ANIM_SEQUENCE = ["idle", "idle", "idle", "blink", "idle", 
                 "look_left", "look_left", "look_right", "look_right", "idle"]

class EvolvingDashboard:
    def __init__(self):
        self.logs = ["Starting CLI Evolving Mascot..."]
        self.frame_idx = 0
        self.evolution_level = 1 
        
    def generate_layout(self):
        current_level_frames = EVOLUTION_STATES[self.evolution_level]
        action_name = ANIM_SEQUENCE[self.frame_idx % len(ANIM_SEQUENCE)]
        mascot_text = Text.from_ansi(current_level_frames[action_name])
        
        status_text = Text(f"Mascot Level: {self.evolution_level}", style="bold magenta")
        
        log_text = Text.from_ansi("\n".join(self.logs[-8:]))
        log_panel = Panel(log_text, title="System Logs", width=55, border_style="blue")
        
        return Group(mascot_text, status_text, log_panel)

dash = EvolvingDashboard()

def animation_worker(live_display):
    while True:
        dash.frame_idx += 1
        live_display.update(dash.generate_layout())
        time.sleep(0.3)

if __name__ == "__main__":
    with Live(dash.generate_layout(), refresh_per_second=10) as live:
        
        anim_thread = threading.Thread(target=animation_worker, args=(live,), daemon=True)
        anim_thread.start()
        
        # PHASE 1
        for i in range(1, 3):
            time.sleep(2) 
            dash.logs.append(f"[\033[92mSUCCESS\033[0m] Initializing module {i}/6...")
        
        dash.logs.append("[\033[93mEVENT\033[0m] Base initialization complete. \033[93mEVOLVING!\033[0m")
        dash.evolution_level = 2
        time.sleep(1) 

        # PHASE 2
        for i in range(3, 5):
            time.sleep(2)
            dash.logs.append(f"[\033[92mSUCCESS\033[0m] Compiling core {i}/6...")

        dash.logs.append("[\033[95mEVENT\033[0m] Cores compiled. \033[95mMAX EVOLUTION REACHED!\033[0m")
        dash.evolution_level = 3
        time.sleep(1)

        # PHASE 3
        for i in range(5, 7):
            time.sleep(2)
            dash.logs.append(f"[\033[92mSUCCESS\033[0m] Finalizing module {i}/6...")
            
        dash.logs.append("[\033[92mCOMPLETE\033[0m] System fully deployed. Mascot is ready.")
        time.sleep(4)