import io
from copy import deepcopy
from typing import List

from nougat import NougatModel
from nougat.utils.checkpoint import get_checkpoint
import re
from PIL import Image, ImageDraw
import fitz as pymupdf
from marker.bbox import should_merge_blocks, merge_boxes, multiple_boxes_intersect
from marker.settings import settings
from marker.schema import Page, Span, Line, Block, BlockType
from nougat.utils.device import move_to_device


def load_model():
    ckpt = get_checkpoint(None, model_tag="0.1.0-small")
    nougat_model = NougatModel.from_pretrained(ckpt)
    if settings.TORCH_DEVICE != "cpu":
        is_cuda = "cuda" in settings.TORCH_DEVICE
        move_to_device(nougat_model, bf16=is_cuda, cuda=is_cuda)
    nougat_model.eval()
    return nougat_model


nougat_model = load_model()
MODEL_MAX = nougat_model.config.max_length

NOUGAT_HALLUCINATION_WORDS = ["[MISSING_PAGE_POST]", "## References\n", "**Figure Captions**\n", "Footnote", "\par\par\par", "## Chapter", "Fig."]


def contains_equation(text):
    # Define a regular expression pattern to look for operators and symbols commonly found in equations
    pattern = re.compile(r'[=\^\√∑∏∫∂∆π≈≠≤≥∞∩∪∈∉∀∃∅∇λμσαβγδεζηθφχψω]')
    # Search the text for the pattern
    match = pattern.search(text)

    # Alternative equation patterns
    alt_pattern = re.compile(r' P(?=[ \n\(\)$])')
    alt_match = alt_pattern.search(text)
    # Return True if the pattern is found, otherwise return False
    return bool(match) or bool(alt_match)


def mask_bbox(png_image, bbox, selected_bboxes):
    mask = Image.new('L', png_image.size, 0)  # 'L' mode for grayscale
    draw = ImageDraw.Draw(mask)
    first_x = bbox[0]
    first_y = bbox[1]
    bbox_height = bbox[3] - bbox[1]
    bbox_width = bbox[2] - bbox[0]

    for box in selected_bboxes:
        # Fit the box to the selected region
        new_box = (box[0] - first_x, box[1] - first_y, box[2] - first_x, box[3] - first_y)
        # Fit mask to image bounds versus the pdf bounds
        resized = (
           new_box[0] / bbox_width * png_image.size[0],
           new_box[1] / bbox_height * png_image.size[1],
           new_box[2] / bbox_width * png_image.size[0],
           new_box[3] / bbox_height * png_image.size[1]
        )
        draw.rectangle(resized, fill=255)

    result = Image.composite(png_image, Image.new('RGBA', png_image.size, 'white'), mask)
    return result


def get_nougat_text(page, old_text, bbox, selected_bboxes, save_id, max_length=MODEL_MAX):
    pix = page.get_pixmap(dpi=settings.DPI, clip=bbox)
    png = pix.pil_tobytes(format="PNG")
    png_image = Image.open(io.BytesIO(png))
    png_image = mask_bbox(png_image, bbox, selected_bboxes)

    nougat_model.config.max_length = min(max_length, MODEL_MAX)
    output = nougat_model.inference(image=png_image)
    return output["predictions"][0]


def replace_equations(doc, blocks: List[Page], block_types: List[List[BlockType]]):
    span_id = 0
    new_blocks = []
    for pnum, page in enumerate(blocks):
        i = 0
        new_page_blocks = []
        equation_boxes = [b.bbox for b in block_types[pnum] if b.block_type == "Formula"]
        while i < len(page.blocks):
            block = page.blocks[i]
            block_text = block.prelim_text
            bbox = block.bbox
            # Check if the block contains an equation
            if not block.contains_equation(equation_boxes):
                new_page_blocks.append(block)
                i += 1
                continue

            selected_blocks = [i]
            if i > 0:
                j = 1
                prev_block = page.blocks[i - j]
                prev_bbox = prev_block.bbox
                while (should_merge_blocks(prev_bbox, bbox) or prev_block.contains_equation(equation_boxes)) and i - j >= 0:
                    bbox = merge_boxes(prev_bbox, bbox)
                    prev_block = page.blocks[i - j]
                    prev_bbox = prev_block.bbox
                    block_text = prev_block.prelim_text + " " + block_text
                    new_page_blocks = new_page_blocks[:-1]  # Remove the previous block, since we're merging it in
                    j += 1
                    selected_blocks.append(i - j)

            if i < len(page.blocks) - 1:
                next_block = page.blocks[i + 1]
                next_bbox = next_block.bbox
                while (should_merge_blocks(bbox, next_bbox) or next_block.contains_equation(equation_boxes)) and i + 1 < len(page.blocks):
                    bbox = merge_boxes(bbox, next_bbox)
                    block_text += " " + next_block.prelim_text
                    i += 1
                    selected_blocks.append(i)
                    if i + 1 < len(page.blocks):
                        next_block = page.blocks[i + 1]
                        next_bbox = next_block.bbox

            used_nougat = False
            if len(block_text) < 2000:
                selected_bboxes = [page.blocks[i].bbox for i in selected_blocks]
                # This prevents hallucinations from running on for a long time
                max_tokens = len(block_text) + 50
                max_char_length = 2 * len(block_text) + 100
                nougat_text = get_nougat_text(doc[pnum], block_text, bbox, selected_bboxes, f"{pnum}_{i}", max_length=max_tokens)
                conditions = [
                    len(nougat_text) > 0,
                    not any([word in nougat_text for word in NOUGAT_HALLUCINATION_WORDS]),
                    len(nougat_text) < max_char_length, # Reduce hallucinations
                    len(nougat_text) >= len(block_text) * .8
                ]
                if all(conditions):
                    block_line = Line(
                        spans=[
                            Span(
                                text=nougat_text,
                                bbox=bbox,
                                span_id=f"{pnum}_{span_id}_fixeq",
                                font="Latex",
                                color=0,
                                block_type="Formula"
                            )
                        ],
                        bbox=bbox
                    )
                    new_page_blocks.append(Block(
                        lines=[block_line],
                        bbox=bbox,
                        pnum=pnum
                    ))
                    used_nougat = True
                    span_id += 1

            if not used_nougat:
                for block_idx in selected_blocks:
                    new_page_blocks.append(page.blocks[block_idx])

            i += 1
        # Assign back to page
        new_page = deepcopy(page)
        new_page.blocks = new_page_blocks
        new_blocks.append(new_page)
    return new_blocks