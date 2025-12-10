# analyze

Lets update the python script to analyse the log.  The log remains the same with the addition of 

- Stopped: {"ts":1765298647,"stopped":true}
- Startup: {"ts":integer,"restart":true}
- Change of state: {"ts":integer,"idle":boolean,"locked":boolean,"windows":array of objects}
  - if the system has entered the idle state since the last log line, idle: true
  - if the system has entered the locked state since the last log line, locked: true
  - the window objects have two forms: 
	- {"pid":integer,"focused":true,"title":window title,"hash":"<ts>-<4 char hash>"}
    - {"pid":integer,"focused":boolean,"hash":"<ts>-<4 char hash>"}
	
I would like to enhance the previous script you wrote for me to NOT include idle, stopped, or locked time in a window's cumulative time.
In addition a report indicating total idle time, total stopped time, and total locked time at the end of the table describing the windows.
