"""Export every saved group, then compare the actual DOCX paragraphs in full."""
from io import StringIO
from itertools import zip_longest
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile
import os
import re
import uuid

from number_app.storage.locking import WorkspaceLock
from .access import require_usable
from .catalog import Catalog
from .output_paths import protect_paths
from .protocol import assert_snapshot_unchanged, digest, read_snapshot

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_LINE = re.compile(r'[0-9]{3}(?:\.[0-9]{3}){0,4}直选[1-9][0-9]*元', re.ASCII)


class WordExportCancelled(ValueError):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise WordExportCancelled('Word导出已取消，未发布文件')


def _chinese_number(number):
    """Group numbers can exceed ten thousand; retain every decimal place."""
    if type(number) is not int or not 1 <= number < 100000000:
        raise ValueError('不支持的Word组号')
    digits = '零一二三四五六七八九'
    def section(value):
        text, pending_zero = '', False
        for unit, label in ((1000, '千'), (100, '百'), (10, '十'), (1, '')):
            digit, value = divmod(value, unit)
            if digit:
                if pending_zero:
                    text += '零'
                text += digits[digit] + label
                pending_zero = False
            elif text and value:
                pending_zero = True
        return text
    high, low = divmod(number, 10000)
    result = (section(high) + '万' if high else '')
    if high and 0 < low < 1000:
        result += '零'
    if low:
        result += section(low)
    return result[1:] if result.startswith('一十') else result


def _groups(source_text, group_count):
    """Read all groups from the immutable saved TXT, without reformatting bodies."""
    source = StringIO(source_text)
    for number in range(1, group_count + 1):
        if source.readline() != f'第{number}组\n':
            raise ValueError(f'原始结果第{number}组缺失或顺序错误')
        lines = []
        while True:
            line = source.readline()
            if line in ('', '\n'):
                break
            if not line.endswith('\n') or not _LINE.fullmatch(line[:-1]):
                raise ValueError(f'原始结果第{number}组格式错误')
            lines.append(line[:-1])
            if len(lines) > 5:
                raise ValueError('原始结果每组超过五行')
        if not lines or (number < group_count and line != '\n') or (number == group_count and line != ''):
            raise ValueError('原始结果组间空行或结尾不正确')
        yield number, tuple(lines)
    if source.read():
        raise ValueError('原始结果包含额外组或内容')


def _paragraphs(source_text, group_count):
    for number, lines in _groups(source_text, group_count):
        if number > 1:
            yield '', 'Normal', False
        yield '第' + _chinese_number(number) + '组', 'GroupLabel', True
        for index, line in enumerate(lines):
            yield line, 'Normal', index < len(lines) - 1


_CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>'''
_ROOT_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
_DOCUMENT_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
_STYLES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W}">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="宋体"/><w:color w:val="000000"/><w:sz w:val="24"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:before="0" w:after="0" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="GroupLabel"><w:name w:val="Group Label"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:color w:val="000000"/></w:rPr></w:style>
</w:styles>'''


def _write_docx(path, source_text, group_count, *, cancel=None, progress=None):
    count = 0
    with Path(path).open('xb') as output:
        with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml', _CONTENT_TYPES)
            archive.writestr('_rels/.rels', _ROOT_RELS)
            archive.writestr('word/_rels/document.xml.rels', _DOCUMENT_RELS)
            archive.writestr('word/styles.xml', _STYLES)
            with archive.open('word/document.xml', 'w') as document:
                document.write((f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W}"><w:body>').encode('utf-8'))
                for text, style, keep_next in _paragraphs(source_text, group_count):
                    if count % 256 == 0:
                        _check_cancel(cancel)
                        if progress:
                            progress({'phase': 'word_writing', 'paragraphs': count})
                    properties = f'<w:pStyle w:val="{style}"/>' + ('<w:keepNext/>' if keep_next else '')
                    paragraph = f'<w:p><w:pPr>{properties}</w:pPr><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
                    document.write(paragraph.encode('utf-8'))
                    count += 1
                # Letter portrait, one inch margins; automatic line wrapping.
                document.write(b'<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>')
        output.flush()
        os.fsync(output.fileno())
    _check_cancel(cancel)
    return {'group_count': group_count, 'paragraph_count': count}


def _verify_docx(path, source_text, group_count, *, cancel=None, progress=None):
    """A separate XML parser compares every paragraph, including the final one."""
    def actual_paragraphs():
        with ZipFile(path, 'r') as archive:
            if set(archive.namelist()) != {'[Content_Types].xml', '_rels/.rels', 'word/_rels/document.xml.rels', 'word/styles.xml', 'word/document.xml'}:
                raise ValueError('Word文件内容集合不正确')
            with archive.open('word/document.xml') as document:
                for _, element in ET.iterparse(document, events=('end',)):
                    if element.tag == '{' + W + '}p':
                        yield ''.join(item.text or '' for item in element.iter('{' + W + '}t'))
                        element.clear()
    # Read expected body lines directly, independent of the writer's paragraph generator.
    def expected_paragraphs():
        for index, block in enumerate(source_text.removesuffix('\n').split('\n\n'), 1):
            lines = block.split('\n')
            if lines[0] != f'第{index}组' or len(lines) < 2:
                raise ValueError('Word核对的原始组号不连续')
            if index > 1:
                yield ''
            yield '第' + _chinese_number(index) + '组'
            yield from lines[1:]
        if index != group_count:
            raise ValueError('Word核对的原始组数不符')
    missing = object()
    paragraphs = groups = 0
    for actual, expected in zip_longest(actual_paragraphs(), expected_paragraphs(), fillvalue=missing):
        if paragraphs % 256 == 0:
            _check_cancel(cancel)
            if progress:
                progress({'phase': 'word_readback', 'paragraphs': paragraphs})
        if actual != expected:
            raise ValueError(f'Word回读第{paragraphs + 1}段与原结果不同，拒绝发布')
        if actual.startswith('第') and actual.endswith('组'):
            groups += 1
        paragraphs += 1
    if groups != group_count:
        raise ValueError('Word回读总组数不符，拒绝发布')
    _check_cancel(cancel)
    return {'group_count': groups, 'paragraph_count': paragraphs}


def export_amount_word(data_root, batch_id, destination, *, cancel=None, progress=None):
    """Only an anchored, completed batch can be exported; original files stay intact."""
    final = Path(destination)
    if final.suffix.lower() != '.docx':
        raise ValueError('请选择.docx格式的Word文件')
    temporary = final.with_name('.' + final.name + '.' + uuid.uuid4().hex + '.partial')
    root, final, temporary = protect_paths(data_root, final, temporary, workspace_roots=(data_root,), file_paths=(final, temporary))
    if not (root / 'history.sqlite').is_file():
        raise ValueError('请选择已有历史记录的数据目录')
    if final.exists():
        raise FileExistsError('Word文件已存在，请使用新的文件名，避免覆盖')
    _check_cancel(cancel)
    with WorkspaceLock(root):
        catalog = Catalog(root)
        context = require_usable(catalog, batch_id)
        source = catalog.path(batch_id)
        source_text = read_snapshot(source, context)['groups.txt'].decode('utf-8')
        def source_still_trusted():
            try:
                assert_snapshot_unchanged(source, context)
            except (OSError, ValueError) as error:
                catalog.transition(batch_id, 'DAMAGED', error=str(error))
                raise
            # Catalog assigns a fresh transient audit_id on each context read.
            # Compare the persistent bindings after normalizing that identifier.
            current_context = require_usable(catalog, batch_id)
            if current_context.with_audit(context.audit_id) != context:
                raise ValueError('Word导出期间原批次可信信息发生变化')
        final.parent.mkdir(parents=True, exist_ok=True)
        published = False
        try:
            _write_docx(temporary, source_text, context.expected_group_count, cancel=cancel, progress=progress)
            checked = _verify_docx(temporary, source_text, context.expected_group_count, cancel=cancel, progress=progress)
            sha = digest(temporary.read_bytes())
            source_still_trusted()
            _check_cancel(cancel)
            # Windows rename fails if a destination appeared during export.
            if final.exists():
                raise FileExistsError('Word目标文件已存在，拒绝覆盖')
            temporary.rename(final)
            published = True
            if digest(final.read_bytes()) != sha:
                raise ValueError('Word发布后文件指纹不同')
            source_still_trusted()
        except BaseException:
            if published:
                final.rename(temporary)
            if temporary.exists():
                temporary.unlink()
            raise
        return {'status': 'WORD_EXPORTED', 'batch_id': batch_id, 'path': str(final), **checked,
                'readback_verified': True, 'sha256': sha, 'snapshot_digest': context.expected_manifest_sha256}
