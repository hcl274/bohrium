#!/usr/bin/env python3
"""Create a synthetic input for the documented invocation example, not an official case."""
import argparse
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    args.out.parent.mkdir(parents=True,exist_ok=True)
    document=fitz.open();page=document.new_page(width=600,height=800)
    page.insert_text((45,65),"Two column example",fontsize=20,fontname="hebo")
    # Write right column first to make the native content order intentionally non-reading-order.
    for prefix,x in [("RIGHT",330),("LEFT",50)]:
        for i in range(1,9):
            page.insert_text((x,110+18*i),f"{prefix} {i:02d}: observed sample count.",fontsize=10)
    page.insert_text((50,430),"Table 1. Example measurements",fontsize=11)
    for y in [450,485,520]:page.draw_line((50,y),(450,y))
    for x in [50,250,450]:page.draw_line((x,450),(x,520))
    for x,y,text in [(60,473,"Group"),(260,473,"Count"),(60,508,"Reference"),(260,508,"42")]:
        page.insert_text((x,y),text,fontsize=11)
    document.set_metadata({"title":"Synthetic invocation example","subject":"Example only"})
    document.save(args.out);document.close()
    print(str(args.out.resolve()))


if __name__=="__main__":main()
