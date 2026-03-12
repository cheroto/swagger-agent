import time
import threading
import re
from rich.live import Live
from rich.text import Text
from rich.console import Group
from rich.panel import Panel

# ANSI Color Codes
COLORS = {
    '{B}': '\033[94m', # Light Blue (Body)
    '{Y}': '\033[93m', # Light Yellow (Horns)
    '{W}': '\033[97m', # White (Eye frame/Shine)
    '{C}': '\033[96m', # Cyan (Pupil)
    '{R}': '\033[91m', # Bright Red (Mouth)
    '{G}': '\033[92m', # Green (Bronze Armor)
    '{M}': '\033[95m', # Magenta (Steel/Mythic Armor)
    '{X}': '\033[0m'   # Reset
}

# --- 1. Define Base Mascot Frames ---
BASE_FRAMES = {
    "idle": r"""
        {Y}/\{B}        {Y}/\{B}    
       {Y}/{B}  \______/  {Y}\{B}   
      |   {W}.------.{B}   |  
      |  {W}|{B}  {C}[+]{B}   {W}|{B}  |  
      |   {W}'------'{B}   |  
      |  {R}.--------.{B}  |  
     /|  {R}\________/{B}  |\ 
    |_|              |_|
      |______________|  
        | |      | |    
        |_|      |_|    
    """,
    "look_left": r"""
        {Y}/\{B}        {Y}/\{B}    
       {Y}/{B}  \______/  {Y}\{B}   
      |   {W}.------.{B}   |  
      |  {W}|{B} {C}[+]{B}    {W}|{B}  |  
      |   {W}'------'{B}   |  
      |  {R}.--------.{B}  |  
     /|  {R}\________/{B}  |\ 
    |_|              |_|
      |______________|  
        | |      | |    
        |_|      |_|    
    """,
    "look_right": r"""
        {Y}/\{B}        {Y}/\{B}    
       {Y}/{B}  \______/  {Y}\{B}   
      |   {W}.------.{B}   |  
      |  {W}|{B}    {C}[+]{B} {W}|{B}  |  
      |   {W}'------'{B}   |  
      |  {R}.--------.{B}  |  
     /|  {R}\________/{B}  |\ 
    |_|              |_|
      |______________|  
        | |      | |    
        |_|      |_|    
    """,
    "blink": r"""
        {Y}/\{B}        {Y}/\{B}    
       {Y}/{B}  \______/  {Y}\{B}   
      |              |  
      |  {W}>--------<{B}  |  
      |              |  
      |  {R}.--------.{B}  |  
     /|  {R}\________/{B}  |\ 
    |_|              |_|
      |______________|  
        | |      | |    
        |_|      |_|    
    """
}

# --- 2. Define Evolution Overlays (Equipment) ---
# Each line perfectly aligns with the base frame's width to avoid deleting his horns/arms.
# We use raw strings (r"") to prevent Python SyntaxWarnings.
OVERLAYS = {
    "bronze_helm": {
        1: r"       {Y}/{B}   {G}.__.{B}   {Y}\{B}   ", 
    },
    "bronze_armor": {
        7: r"    |_|   {G}[======]{B}   |_|",
        8: r"      |___{G}[======]{B}___|  ",
    },
    "steel_helm": {
        1: r"       {Y}/{B}   {M}/TTTT\{B}   {Y}\{B}   ",
    },
    "steel_armor": {
        7: r"    |_|   {M}[XXXXXX]{B}   |_|",
        8: r"      |___{M}[XXXXXX]{B}___|  ",
    },
    "sword_right": {
        # Appends the sword to the right side, while retaining the Steel Armor in the middle
        4: r"      |   {W}'------'{B}   |     {M}/{B}",
        5: r"      |  {R}.--------.{B}  |    {M}/|{B}",
        6: r"     /|  {R}\________/{B}  |\  {M}/ |{B}",
        7: r"    |_|   {M}[XXXXXX]{B}   |_| {M}|  |{B}", 
        8: r"      |___{M}[XXXXXX]{B}___|   {M}|  |{B}", 
        9: r"        | |      | |     {M}|/ {B}",
       10: r"        |_|      |_|     {M}V  {B}",
    }
}

# --- 3. Process and Store Evolution States ---
EVOLUTION_STATES = {}

def apply_overlays(base_frame_str, overlay_keys):
    base_lines = base_frame_str.strip('\n').split('\n')
    for overlay_key in overlay_keys:
        if overlay_key in OVERLAYS:
            for line_idx, overlay_content in OVERLAYS[overlay_key].items():
                if 0 <= line_idx < len(base_lines):
                    base_lines[line_idx] = overlay_content
    return '\n'.join(base_lines)

def pre_process_animations():
    width = 32 # Increased width slightly to accommodate the sword safely
    
    levels = {
        1: [], 
        2: ["bronze_helm", "bronze_armor"], 
        3: ["steel_helm", "steel_armor", "sword_right"] 
    }
    
    for level, gear_list in levels.items():
        processed_level_frames = {}
        for action, base_frame_str in BASE_FRAMES.items():
            geared_frame_str = apply_overlays(base_frame_str, gear_list)
            
            frame_lines = []
            lines = geared_frame_str.split('\n')
            for line in lines:
                visual_line = re.sub(r'\{[A-Z]\}', '', line)
                padding_needed = max(0, width - len(visual_line))
                padded_line = '{B}' + line + ' ' * padding_needed + '{X}'
                for tag, ansi_code in COLORS.items():
                    padded_line = padded_line.replace(tag, ansi_code)
                frame_lines.append(padded_line)
            processed_level_frames[action] = '\n'.join(frame_lines)
            
        EVOLUTION_STATES[level] = processed_level_frames

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
        
        status_text = Text(f"Mascot Level: {self.evolution_level}", style="bold yellow")
        
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
            live.update(dash.generate_layout())

        # *** EVOLUTION TRIGGER 1 ***
        dash.logs.append("[\033[93mEVENT\033[0m] Base initialization complete. \033[93mEVOLVING!\033[0m")
        dash.evolution_level = 2
        live.update(dash.generate_layout())
        time.sleep(1) 

        # PHASE 2
        for i in range(3, 5):
            time.sleep(2)
            dash.logs.append(f"[\033[92mSUCCESS\033[0m] Compiling core {i}/6...")
            live.update(dash.generate_layout())

        # *** EVOLUTION TRIGGER 2 ***
        dash.logs.append("[\033[93mEVENT\033[0m] Cores compiled. \033[93mMAX EVOLUTION REACHED!\033[0m")
        dash.evolution_level = 3
        live.update(dash.generate_layout())
        time.sleep(1)

        # PHASE 3
        for i in range(5, 7):
            time.sleep(2)
            dash.logs.append(f"[\033[92mSUCCESS\033[0m] Finalizing module {i}/6...")
            live.update(dash.generate_layout())
            
        dash.logs.append("[\033[92mCOMPLETE\033[0m] System fully deployed. Mascot is ready.")
        live.update(dash.generate_layout())
        time.sleep(3)