Monaco Editor хранится локально, чтобы приложение могло работать без интернета.

В этой сборке app.js ищет AMD-loader по адресу:
  /static/vendor/monaco/loader.js
и подключает модуль:
  /static/vendor/monaco/editor/editor.main.js

Worker-файлы используются из:
  /static/vendor/monaco/assets/

Если заменяете Monaco на другую сборку, проверьте пути в функции initMonacoEditor() файла static/app.js.
