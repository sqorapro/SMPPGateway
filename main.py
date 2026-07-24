import sys
import os

if getattr(sys, 'frozen', False):
    base = sys._MEIPASS
else:
    base = os.path.dirname(os.path.abspath(__file__))

src_path = os.path.join(base, 'src')
sys.path.insert(0, src_path)
sys.path.insert(0, base)

from gui.main_window import main

if __name__ == "__main__":
    main()
