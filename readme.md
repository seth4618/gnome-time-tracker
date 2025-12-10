# Track acitivty under gnome with wayland

Track focus on windows.  Eventually, set up focus times using 45-on/15-off method.

# list extensions to make sure it’s seen

gnome-extensions list | grep window-logger

# enable it

gnome-extensions enable window-logger@example.com

# check that it is ok:

gnome-extensions info window-logger@example.com

# reloading under wayland

you have to logout/login to reload.

