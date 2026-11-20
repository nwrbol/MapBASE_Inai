"""Исключения ядра: всё, что связано с некорректным входом, наследует DataValidationError."""


class CoverageError(Exception):
    """Базовое исключение пакета coverage."""


class DataValidationError(CoverageError):
    """Входные данные нельзя обработать (нет файла, нет столбцов, пустой файл, кодировка)."""


class ConfigError(CoverageError):
    """Некорректный config.yaml или переопределение параметров."""
