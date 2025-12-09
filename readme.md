# reload GNOME Shell extensions (Xorg: Alt+F2, type r, Enter; on Wayland you must log out/in)

# list extensions to make sure it’s seen
gnome-extensions list | grep window-logger

# enable it
gnome-extensions enable window-logger@example.com
