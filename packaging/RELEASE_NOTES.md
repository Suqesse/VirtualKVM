## Скачать VirtualKVM {{VERSION}}

| Система | Архитектура | Установщик | Образ диска |
| --- | --- | --- | --- |
| Windows | x64 · Intel / AMD | [Скачать EXE](https://github.com/{{REPOSITORY}}/releases/download/v{{VERSION}}/VirtualKVM-{{VERSION}}-windows-x64-setup.exe) | — |
| macOS 15 и новее | Apple Silicon · M1 и новее | [Скачать PKG](https://github.com/{{REPOSITORY}}/releases/download/v{{VERSION}}/VirtualKVM-{{VERSION}}-macos-arm64.pkg) | [Скачать DMG](https://github.com/{{REPOSITORY}}/releases/download/v{{VERSION}}/VirtualKVM-{{VERSION}}-macos-arm64.dmg) |
| macOS 15 и новее | Intel · x86_64 | [Скачать PKG](https://github.com/{{REPOSITORY}}/releases/download/v{{VERSION}}/VirtualKVM-{{VERSION}}-macos-x86_64.pkg) | [Скачать DMG](https://github.com/{{REPOSITORY}}/releases/download/v{{VERSION}}/VirtualKVM-{{VERSION}}-macos-x86_64.dmg) |

Python, Qt, FFmpeg и ffprobe уже включены. Дополнительно устанавливать их не нужно. Для Mac выберите **один** вариант: мастер установки PKG или DMG с переносом приложения в «Программы».

[Инструкция по установке](https://github.com/{{REPOSITORY}}/blob/v{{VERSION}}/docs/INSTALLERS.md) · [Руководство](https://github.com/{{REPOSITORY}}/blob/v{{VERSION}}/docs/GUIDE.md) · [SHA-256](https://github.com/{{REPOSITORY}}/releases/download/v{{VERSION}}/SHA256SUMS)

## Исправления в 0.2.1

- Если Windows запрещает выбранный TCP-порт с кодом 10013 или порт уже занят, интерфейс запрашивает доступный порт у ОС и показывает его реальный номер.
- На Windows применяется эксклюзивное владение сокетом хоста.
- Для короткого PIN показана подсказка до запуска, кнопка запуска доступна после исправления.

## Возможности 0.2.0

- Минималистичный светлый интерфейс со спокойными цветами, без слоганов.
- Настройка от 1 до 8 экранов: перетаскивание, точные X/Y, готовые схемы расположения.
- Назначение источников отдельным локальным мониторам и полноэкранный режим.
- Сохранение схемы и перенос прежних настроек двух экранов.
- Сохранение пропорций горизонтальных и портретных экранов, учёт ориентации Retina.
- Проверка интерфейса после установки; повтор сборки DMG при временной занятости DiskImages.

## Проверки

Установщики прошли нативные проверки на **Windows x64, Mac Apple Silicon и Mac Intel**: установка, интерфейс и ресурсы, автономные видео и звук, обновление с сохранением пользовательских данных. Дополнительно проверены удаление на Windows и содержимое DMG на macOS.

[Отчёт установок и точный коммит исходников](https://github.com/{{REPOSITORY}}/releases/download/v{{VERSION}}/installation-checks.json) · [Матрица тестирования](https://github.com/{{REPOSITORY}}/blob/v{{VERSION}}/docs/TESTING.md)

## Перед первым запуском

Это пилотная версия без доверенных подписей Apple Developer ID / Windows Authenticode и без нотарификации Apple. Разрешения на запись экрана, ввод и выбранное аудиоустройство выдаются обычным способом в настройках ОС. Установка сама не запускает общий доступ.

Приложение передаёт существующие экраны хоста; новые виртуальные дисплеи в ОС не создаются. **240 FPS — настраиваемая цель**: фактические захват, показ, звук и задержка зависят от оборудования и требуют измерений на реальных компьютерах.

Код приложения — MIT. Лицензии включённых компонентов находятся в каталоге `licenses` установленного приложения.
