"""pytest 引导：把仓库根目录加入 sys.path，使 `import app` 在裸 pytest 下可用。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
