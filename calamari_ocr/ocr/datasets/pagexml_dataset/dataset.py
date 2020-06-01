import itertools
from string import whitespace
import re
import os
import numpy as np
from PIL import Image
from tqdm import tqdm
from lxml import etree
from skimage.draw import polygon
from skimage.transform import rotate
from typing import List

from calamari_ocr.ocr.datasets import DataSet, DataSetMode, DatasetGenerator
from calamari_ocr.utils import split_all_ext, filename

import logging
logger = logging.getLogger(__name__)


def xml_attr(elem, ns, label, default=None):
    try:
        return elem.xpath(label, namespaces=ns).pop()
    except IndexError as e:
        if default is None:
            raise e

        return default


class PageXMLDatasetGenerator(DatasetGenerator):
    def __init__(self, mp_context, output_queue, mode: DataSetMode, images, xml_files, non_existing_as_empty, text_index, skip_invalid, args):
        super().__init__(mp_context, output_queue, mode, list(zip(images, xml_files)))
        self._non_existing_as_empty = non_existing_as_empty
        self.text_index = text_index
        self.skip_invalid = skip_invalid
        self.args = args

    def _load_sample(self, sample, text_only):
        loader = PageXMLDatasetLoader(self.mode, self._non_existing_as_empty, self.text_index, self.skip_invalid)
        image_path, xml_path = sample

        img = None
        if self.mode == DataSetMode.PREDICT or self.mode == DataSetMode.TRAIN or self.mode == DataSetMode.PRED_AND_EVAL:
            img = np.array(Image.open(image_path))

        for sample in loader.load(image_path, xml_path):
            text = sample["text"]
            orientation = sample["orientation"]

            if not text_only and (self.mode == DataSetMode.PREDICT or self.mode == DataSetMode.TRAIN or self.mode == DataSetMode.PRED_AND_EVAL):
                ly, lx = img.shape[:2]

                line_img = PageXMLDataset.cutout(img, sample['coords'], lx / sample['img_width'])

                # rotate by orientation angle in clockwise direction to correct present skew
                # (skimage rotates in counter-clockwise direction)
                if orientation and orientation % 360 != 0:
                    line_img = rotate(line_img, orientation*-1, resize=True, mode='constant', cval=line_img.max(), preserve_range=True).astype(np.uint8)

                # add padding as required from normal files
                if self.args.get('pad', None):
                    pad = self.args['pad']
                    img = np.pad(img, pad, mode='constant', constant_values=img.max())
            else:
                line_img = None

            yield line_img, text


class PageXMLDatasetLoader:
    def __init__(self, mode: DataSetMode, non_existing_as_empty: bool, text_index: int, skip_invalid: bool=True):
        self.mode = mode
        self._non_existing_as_empty = non_existing_as_empty
        self.root = None
        self.text_index = text_index
        self.skip_invalid = skip_invalid

    def load(self, img, xml, skip_commented=True):
        if not os.path.exists(xml):
            if self._non_existing_as_empty:
                return None
            else:
                raise Exception("File '{}' does not exist.".format(xml))

        root = etree.parse(xml).getroot()
        self.root = root

        if self.mode == DataSetMode.TRAIN or self.mode == DataSetMode.EVAL or self.mode == DataSetMode.PRED_AND_EVAL:
            return self._samples_gt_from_book(root, img, skip_commented, xml)
        else:
            return self._samples_from_book(root, img, xml)

    def _samples_gt_from_book(self, root, img, page_id,
                              skipcommented=True):
        ns = {"ns": root.nsmap[None]}
        imgfile = root.xpath('//ns:Page',
                             namespaces=ns)[0].attrib["imageFilename"]
        if (self.mode == DataSetMode.TRAIN or self.mode == DataSetMode.PRED_AND_EVAL) and not split_all_ext(img)[0].endswith(split_all_ext(imgfile)[0]):
            raise Exception("Mapping of image file to xml file invalid: {} vs {} (comparing basename {} vs {})".format(
                img, imgfile, split_all_ext(img)[0], split_all_ext(imgfile)[0]))

        img_w = int(root.xpath('//ns:Page',
                               namespaces=ns)[0].attrib["imageWidth"])
        textlines = root.xpath('//ns:TextLine', namespaces=ns)

        for textline in textlines:
            tequivs = textline.xpath('./ns:TextEquiv[@index="{}"]'.format(self.text_index),
                                namespaces=ns)

            if not tequivs:
                tequivs = textline.xpath('./ns:TextEquiv[not(@index)]', namespaces=ns)

            if len(tequivs) > 1:
                logger.warning("PageXML is invalid: TextLine includes TextEquivs with non unique ids")

            parat = textline.attrib
            if skipcommented and "comments" in parat and parat["comments"]:
                continue

            if tequivs is not None and len(tequivs) > 0:
                l = tequivs[0]
                text = l.xpath('./ns:Unicode', namespaces=ns).pop().text
            else:
                l = None
                text = None

            if not text:
                if self.skip_invalid:
                    continue
                elif self._non_existing_as_empty:
                    text = ""
                else:
                    raise Exception("Empty text field")

            try:
                orientation = float(textline.xpath('../@orientation', namespaces=ns).pop())
            except (ValueError, IndexError):
                orientation = 0

            yield {
                'page_id': page_id,
                'ns': ns,
                "rtype": xml_attr(textline, ns, '../@type', ''),
                'xml_element': l,
                "image_path": img,
                "id": xml_attr(textline, ns, './@id'),
                "text": text,
                "coords": xml_attr(textline, ns, './ns:Coords/@points'),
                "orientation": orientation,
                "img_width": img_w
            }

    def _samples_from_book(self, root, img, page_id):
        ns = {"ns": root.nsmap[None]}
        imgfile = root.xpath('//ns:Page',
                             namespaces=ns)[0].attrib["imageFilename"]
        if not split_all_ext(img)[0].endswith(split_all_ext(imgfile)[0]):
            raise Exception("Mapping of image file to xml file invalid: {} vs {} (comparing basename {} vs {})".format(
                img, imgfile, split_all_ext(img)[0], split_all_ext(imgfile)[0]))

        img_w = int(root.xpath('//ns:Page',
                               namespaces=ns)[0].attrib["imageWidth"])
        for l in root.xpath('//ns:TextLine', namespaces=ns):
            try:
                orientation = float(l.xpath('../@orientation', namespaces=ns).pop())
            except (ValueError, IndexError):
                orientation = 0

            yield {
                'page_id': page_id,
                'ns': ns,
                "rtype": xml_attr(l, ns, '../@type', ''),
                'xml_element': l,
                "image_path": img,
                "id": xml_attr(l, ns, './@id'),
                "coords": xml_attr(l, ns, './ns:Coords/@points'),
                "orientation": orientation,
                "img_width": img_w,
                "text": None,
            }


class PageXMLDataset(DataSet):

    def __init__(self,
                 mode: DataSetMode,
                 files,
                 xmlfiles: List[str] = None,
                 skip_invalid=False,
                 remove_invalid=True,
                 non_existing_as_empty=False,
                 args: dict = None,
                 ):

        """ Create a dataset from a Path as String
        Parameters
         ----------
        files : [], required
            image files
        skip_invalid : bool, optional
            skip invalid files
        remove_invalid : bool, optional
            remove invalid files
        """

        super().__init__(
            mode,
            skip_invalid, remove_invalid,
        )

        if xmlfiles is None:
            xmlfiles = []

        if args is None:
            args = {}

        self.args = args

        self.text_index = args.get('text_index', 0)
        self.word_level = args.get('word_level', 0)
        self.word_boundary = args.get('word_boundary', 'unicode')

        self._non_existing_as_empty = non_existing_as_empty
        if len(xmlfiles) == 0:
            xmlfiles = [split_all_ext(p)[0] + ".xml" for p in files]

        if len(files) == 0:
            files = [None] * len(xmlfiles)

        self.files = files
        self.xmlfiles = xmlfiles
        self.pages = []
        for img, xml in zip(files, xmlfiles):
            loader = PageXMLDatasetLoader(self.mode, self._non_existing_as_empty, self.text_index, self.skip_invalid)
            for sample in loader.load(img, xml):
                self.add_sample(sample)

            self.pages.append(loader.root)

        # store which pagexml was stored last, to check when a file is ready to be written during sequential prediction
        self._last_page_id = None

    @staticmethod
    def cutout(pageimg, coordstring, scale=1, rect=False):
        coords = [p.split(",") for p in coordstring.split()]
        coords = np.array([(int(scale * int(c[1])), int(scale * int(c[0])))
                           for c in coords])
        if rect:
            return pageimg[min(c[0] for c in coords):max(c[0] for c in coords),
                   min(c[1] for c in coords):max(c[1] for c in coords)]
        rr, cc = polygon(coords[:, 0], coords[:, 1], pageimg.shape)
        offset = (min([x[0] for x in coords]), min([x[1] for x in coords]))
        box = np.ones(
            (max([x[0] for x in coords]) - offset[0],
             max([x[1] for x in coords]) - offset[1],
             ) + ((pageimg.shape[-1],) if len(pageimg.shape) == 3 else ()),
            dtype=pageimg.dtype) * 255
        box[rr - offset[0], cc - offset[1]] = pageimg[rr, cc]
        return box

    def get_words(self, prediction, sample) -> list:
        def remove_leading_spaces(_positions):
            return list(itertools.dropwhile(lambda p: p[0][0] in whitespace, _positions))

        def remove_trailing_spaces(_positions) -> list:
            return list(reversed(remove_leading_spaces(reversed(_positions))))

        def is_word_boundary(character: str) -> bool:
            if self.word_boundary == "unicode":
                return bool(re.match(r"\B", character, flags=re.U))
            elif self.word_boundary == "whitespace":
                return character in whitespace
            return False
        x_coords, y_coords = map(list, zip(*[coord.split(",") for coord in sample['coords'].split()]))
        x, y = [int(x) for x in x_coords], [int(y) for y in y_coords]
        min_x, max_x, min_y, max_y = min(x), max(x), min(y), max(y)

        positions = [(pos.chars[0].char, pos.global_start + min_x, pos.global_end + min_x) for pos in
                     prediction.positions]
        positions = remove_leading_spaces(positions)
        positions = remove_trailing_spaces(positions)

        if not positions:
            return []

        words = [{"char": positions[0][0], "min_x": positions[0][1], "max_x": positions[0][2],
                  "min_y": min_y, "max_y": max_y}]
        new_word = False
        word_boundary = False

        for entry in positions[1:]:
            if is_word_boundary(entry[0]):
                if word_boundary:
                    if self.word_boundary != "whitespace":
                        words[-1]["char"] += entry[0]
                    words[-1]["max_x"] = entry[2]
                else:
                    words.append({"char": entry[0], "min_x": entry[1], "max_x": entry[2], "min_y": min_y, "max_y": max_y})
                new_word = True
                word_boundary = True
                continue
            if new_word:
                words.append({"char": entry[0], "min_x": entry[1], "max_x": entry[2], "min_y": min_y, "max_y": max_y})
                new_word = False
            else:
                words[-1]["char"] += entry[0]
                words[-1]["max_x"] = entry[2]
            word_boundary = False

        return words

    def store_words(self, prediction, sample):
        ns = sample['ns']
        line = sample['xml_element']

        line_id = line.attrib["id"]
        textequivelem = line.find("./ns:TextEquiv", namespaces=ns)

        words = self.get_words(prediction, sample)

        if len(textequivelem) and words:
            for index, word in enumerate(words, 1):
                word_elem = etree.Element("{%s}Word" % ns["ns"], nsmap=ns, id=f"{line_id}_w{str(index).zfill(3)}")
                textequivelem.addprevious(word_elem)

                _points = f'{word["min_x"]},{word["max_y"]} {word["max_x"]},{word["max_y"]} {word["max_x"]},{word["min_y"]} {word["min_x"]},{word["min_y"]}'
                etree.SubElement(word_elem, "Coords", attrib={"points": _points})

                word_textequivxml = etree.SubElement(word_elem, "TextEquiv", attrib={"index": str(self.text_index)})

                w_xml = etree.SubElement(word_textequivxml, "Unicode")
                w_xml.text = word["char"]

    def prepare_store(self):
        self._last_page_id = None

    def store_text(self, prediction, sample, output_dir, extension):
        ns = sample['ns']
        line = sample['xml_element']

        for word_elem in line.findall(".//ns:Word", namespaces=ns):
            line.remove(word_elem)

        textequivxml = line.find('./ns:TextEquiv[@index="{}"]'.format(self.text_index),
                                    namespaces=ns)
        if textequivxml is None:
            textequivxml = etree.SubElement(line, "TextEquiv", attrib={"index": str(self.text_index)})

        u_xml = textequivxml.find('./ns:Unicode', namespaces=ns)
        if u_xml is None:
            u_xml = etree.SubElement(textequivxml, "Unicode")

        u_xml.text = prediction.sentence

        if self.word_level and prediction.positions:
            self.store_words(prediction, sample)

        # check if page can be stored, this requires that (standard in prediction) the pages are passed sequentially
        if self._last_page_id != sample['page_id']:
            if self._last_page_id:
                self._store_page(extension, self._last_page_id)
            self._last_page_id = sample['page_id']

    def store_extended_prediction(self, data, sample, output_dir, extension):
        output_dir = os.path.join(output_dir, filename(sample['image_path']))
        if not os.path.exists(output_dir):
            os.mkdir(output_dir)

        super().store_extended_prediction(data, sample, output_dir, extension)

    def store(self, extension):
        if self._last_page_id:
            self._store_page(extension, self._last_page_id)
            self._last_page_id = None
        else:
            for xml, page in tqdm(zip(self.xmlfiles, self.pages), desc="Writing PageXML files", total=len(self.xmlfiles)):
                with open(split_all_ext(xml)[0] + extension, 'w') as f:
                    f.write(etree.tounicode(page.getroottree()))

    def _store_page(self, extension, page_id):
        page = self.pages[self.xmlfiles.index(page_id)]
        with open(split_all_ext(page_id)[0] + extension, 'w') as f:
            f.write(etree.tounicode(page.getroottree()))

    def create_generator(self, mp_context, output_queue) -> DatasetGenerator:
        return PageXMLDatasetGenerator(mp_context, output_queue, self.mode, self.files, self.xmlfiles, self._non_existing_as_empty, self.text_index, self.skip_invalid, self.args)