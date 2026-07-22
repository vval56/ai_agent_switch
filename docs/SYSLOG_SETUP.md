# Настройка отправки syslog с Zyxel GS1920

## Вариант 1: Через веб-интерфейс
1. Откройте http://192.168.3.163
2. **Configuration** → **Diagnostic** → **Log**
3. Поставьте галочку **Logging**
4. В поле **Syslog Host** введите IP машины, где запущен monitor_service
5. Нажмите **Apply**

## Вариант 2: Через CLI
GS1920> enable
GS1920# configure terminal
GS1920(config)# logging host <IP_МАШИНЫ_С_MONITOR_SERVICE>
GS1920(config)# logging on
GS1920(config)# exit
GS1920# write memory

## Проверка
После настройки при любой ошибке (неудачный вход, link down, сброс) в Telegram придет уведомление мгновенно.

## Запуск monitor_service отдельно
python -m src.monitor_service

Или через docker-compose добавьте отдельный сервис.
