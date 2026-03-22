import multiprocessing

# Socket
bind = "unix:/run/sweepmail/sweepmail.sock"

# Workers
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "gthread"
threads = 2
timeout = 120  # Gmail API calls can be slow on large inboxes

# Logging
accesslog = "/var/log/sweepmail/access.log"
errorlog = "/var/log/sweepmail/error.log"
loglevel = "info"

# Process naming
proc_name = "sweepmail"

# Security
umask = 0o007  # socket permissions: owner + group only
