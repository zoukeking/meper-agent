"""Tests for HTTP utilities."""
from urllib.parse import unquote

from app.utils.http import build_content_disposition


class TestBuildContentDisposition:
    """Tests for build_content_disposition."""

    def test_ascii_filename_legacy_form(self) -> None:
        """纯 ASCII 文件名保持 legacy 简单形式"""
        result = build_content_disposition("report.pdf")
        assert result == 'attachment; filename="report.pdf"'

    def test_ascii_filename_no_filename_star(self) -> None:
        """纯 ASCII 文件名不应产生 filename* 参数"""
        result = build_content_disposition("test.txt")
        assert "filename*" not in result

    def test_chinese_filename_emits_filename_star(self) -> None:
        """中文文件名应同时给出 filename 与 RFC 5987 的 filename*"""
        result = build_content_disposition("admin出差申请单.txt")
        assert result.startswith('attachment; filename="')
        assert "filename*=UTF-8''" in result

    def test_chinese_filename_star_roundtrip(self) -> None:
        """filename* 中的 percent-encoding 应能还原为原始文件名"""
        name = "出差申请单.txt"
        result = build_content_disposition(name)
        # 提取 filename*=UTF-8''<...> 中的编码部分
        encoded = result.split("filename*=UTF-8''", 1)[1]
        assert unquote(encoded, encoding="utf-8") == name

    def test_chinese_filename_ascii_fallback(self) -> None:
        """中文文件名的 filename (ASCII fallback) 应为可 latin-1 编码的子集"""
        name = "admin出差申请单.txt"
        result = build_content_disposition(name)
        # 落在双引号之间的 filename 部分应只含 ASCII(中文字符被丢弃)
        fallback = result.split('filename="', 1)[1].split('"', 1)[0]
        fallback.encode("latin-1")  # 不抛异常即通过
        assert "出差申请单" not in fallback
        assert "admin" in fallback
        assert ".txt" in fallback

    def test_pure_chinese_filename(self) -> None:
        """纯中文文件名:ASCII fallback 为空,但仍要有 filename*"""
        name = "总结报告"
        result = build_content_disposition(name)
        assert "filename*=UTF-8''" in result
        # ASCII fallback 部分应为空字符串
        fallback = result.split('filename="', 1)[1].split('"', 1)[0]
        assert fallback == ""
        # filename* 可还原
        encoded = result.split("filename*=UTF-8''", 1)[1]
        assert unquote(encoded, encoding="utf-8") == name

    def test_filename_with_spaces(self) -> None:
        """含空格的 ASCII 文件名"""
        result = build_content_disposition("my report.pdf")
        assert result == 'attachment; filename="my report.pdf"'

    def test_custom_disposition(self) -> None:
        """可指定 inline 等 disposition 类型"""
        result = build_content_disposition("preview.txt", disposition="inline")
        assert result == 'inline; filename="preview.txt"'

    def test_result_is_latin1_encodable(self) -> None:
        """核心保证:返回值必须能被 latin-1 编码(否则 Starlette 仍会崩)"""
        for name in [
            "report.pdf",
            "admin出差申请单.txt",
            " 总结报告 ",
            "café- naïve - résumé.pdf",
            "日本語ファイル.txt",
            "emoji😀.txt",
        ]:
            build_content_disposition(name).encode("latin-1")
