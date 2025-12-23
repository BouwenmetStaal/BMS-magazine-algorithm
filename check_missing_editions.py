from pathlib import Path
import numpy as np

folder_path = Path(r"C:\Users\AJOR\Bouwen met Staal\ChatBmS - General\Archief_BMS_magazines [Erik]\Magazine_compleet_archief")

edition_numbers = []

for subfolder in folder_path.iterdir():
    if subfolder.is_dir():
        print(f"Folder: {subfolder.name}")

for pdf_file in folder_path.rglob('*.pdf'):
    name = pdf_file.name
    number_part = name.split('_')[0]
    edition_numbers.append(number_part)

all_editions = set(range(194, 308))
existing_editions = set(int(num) for num in edition_numbers if num.isdigit())
missing_editions = sorted(all_editions - existing_editions)

print("Missing editions:", missing_editions)

import matplotlib.pyplot as plt

# Create a visual representation
fig, ax = plt.subplots(figsize=(15, 20))

all_editions_list = sorted(all_editions)
colors = ['green' if ed in existing_editions else 'red' for ed in all_editions_list]

# Create long narrow bars for each edition (horizontal)
for i, (edition, color) in enumerate(zip(all_editions_list, colors)):
    ax.add_patch(plt.Rectangle((i, 0), 1, 8, facecolor=color, edgecolor='black'))
    ax.text(i + 0.5, 1.5, "BmS-" + str(edition), ha='center', va='center', fontsize=8, color='white', weight='bold', rotation=90)

# Add horizontal arrows spanning 6 editions
arrow_start = 195
year = 2007
while arrow_start <= max(all_editions_list):
    start_idx = all_editions_list.index(arrow_start) if arrow_start in all_editions_list else None
    end_edition = arrow_start + 5
    end_idx = all_editions_list.index(end_edition) if end_edition in all_editions_list else None
    
    if start_idx is not None and end_idx is not None:
        ax.annotate('', xy=(end_idx + 1, 9), xytext=(start_idx, 9),
               arrowprops=dict(arrowstyle='<->', lw=2, color='black'))
        ax.text((start_idx + end_idx + 1) / 2, 9.5, str(year), ha='center', va='bottom', fontsize=10, weight='bold')
    
    arrow_start += 6
    year += 1

ax.set_xlim(0, len(all_editions_list))
ax.set_ylim(0, 10)
ax.set_aspect('equal')
ax.axis('off')
ax.set_title('BmS Magazine Editions Status', fontsize=12, pad=20)

plt.tight_layout()
plt.show()