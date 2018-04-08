import intel.et
from suricata import *
import datetime, json, logging, os, pprint, queue, signal, smtplib, sys, \
       threading, time


class EventParser(threading.Thread):
    def __init__(self, config, notifier):
        threading.Thread.__init__(self, daemon=False)

        self.stop = False
        self.config = config


    def run(self):
        evefile = open('/var/log/suricata/eve.json', 'r')
        evefile.seek(0, os.SEEK_END)

        alerts_since = time.time()
        alerts_count = 0

        errors_total = []

        while not self.stop:
            line = evefile.readline()
            if not line:
                # Test for log file rotation by comparing the mod.times
                # of the file path and the open file descriptor
                # FIXME Verify this works for next log rotation
                d1 = datetime.datetime.fromtimestamp(
                         os.stat('/var/log/suricata/eve.json').st_mtime)
                d2 = datetime.datetime.fromtimestamp(
                         os.fstat(evefile.fileno()).st_mtime)

                if d1 > d2:
                    logging.info('detected log file rotation, reopening file')
                    evefile = open('/var/log/suricata/eve.json', 'r')
                    evefile.seek(0, os.SEEK_END)
                else:
                    time.sleep(0.1)

                continue

            try:
                record = json.loads(line)
            except:
                # TODO This seems to occur for broken lines,
                #      unclear why since it's not a buffering issue

                logging.exception('line = %r', line)
                # TODO Consider sending an email with the exception

                # Abort if more than 10 errors in 24 hours
                errors_total += [time.time()]
                errors_total = list(filter(lambda t: time.time()-t < 24*60*60,
                                           errors_total))
                if len(errors_total) > 10:
                    self.stop = True

            if record.get('event_type') == 'alert':
                logging.debug('\n' + pprint.pformat(record))
                notifier.alerts.put(record)
                alerts_count += 1

            if time.time() - alerts_since >= 60:
                if alerts_count > 0:
                    logging.info('%u alert%s generated',
                                 alerts_count, '' if alerts_count == 1 else 's')
                alerts_since = time.time()
                alerts_count = 0

            # TODO Consider reporting metrics or accumulated stats (daily?).
            #      Perhaps to start a report on flows (top most contacted IPs).

        evefile.close()


class Notifier(threading.Thread):
    def __init__(self, config):
        threading.Thread.__init__(self, daemon=False)

        self.stop = False
        self.config = config

        self.alerts = queue.Queue()
        self.others = queue.Queue()


    def run(self):
        smtp = smtplib.SMTP(self.config['smtpserver'])

        total = []

        while not self.stop:
            if not self.alerts.empty():
                alerts = [self.alerts.get()]
                while not self.alerts.empty() and len(alerts) < 10:
                    alerts += [self.alerts.get()]

                once = True
                while once:
                    try:
                        smtp.sendmail(
                            self.config['mailtofrom'],
                            self.config['mailtofrom'],
                            '\r\n' + '\n\n'.join(map(lambda a:
                                                         pprint.pformat(a),
                                                     alerts)))

                        logging.info('sent a mail with %u alerts', len(alerts))

                        # Abort if more than 100 alerts (not mails) in 24 hours
                        total += [time.time() * len(alerts)]
                        total = list(filter(lambda t: time.time()-t < 24*60*60,
                                            total))
                        if len(total) > 100:
                            self.stop = True

                        break

                    # SMTP command timeout, broken pipe, etc
                    except smtplib.SMTPException:
                        once = False
                        smtp = smtplib.SMTP(self.config['smtpserver'])

                for i in range(len(alerts)):
                    self.alerts.task_done()

            elif not self.others.empty():
                once = True
                while once:
                    try:
                        smtp.sendmail(self.config['mailtofrom'],
                                      self.config['mailtofrom'],
                                      self.others.get())
                        break
                    except smtplib.SMTPSenderRefused:
                        once = False
                        smtp = smtplib.SMTP(self.config['smtpserver'])

                self.others.task_done()

            else:
                time.sleep(1)

        smtp.quit()


class Downloader(threading.Thread):
    def __init__(self, config):
        threading.Thread.__init__(self, daemon=False)

        self.stop = False
        self.config = config


    def run(self):
        lastupdate = {}

        last = intel.et.install(config, self)
        if last:
            lastupdate['et'] = last
            if not suricata_reloadrules():
                self.stop = True
        else:
            self.stop = True

        since = time.time()

        while not self.stop:
            time.sleep(1)

            if time.time() - since >= 3600:
                last = intel.et.latest(config)
                logging.info('et last modified: %s', last)

                if last > lastupdate['et']:
                    last2 = intel.et.install(config, self)
                    if last2:
                        lastupdate['et'] = last
                        if not suricata_reloadrules():
                            # TODO Consider sending an email about the failure
                            pass
                    else:
                        # TODO Consider sending an email about the failure
                        pass

                since = time.time()


if __name__ == '__main__':
    def stop(signum, frame):
        logging.info('received signal %d', signum)

        for t in threads:
            t.stop = True
            t.join()


    logging.basicConfig(level=logging.INFO,
                        format=('%(asctime)s:%(levelname)s:%(name)s:' + \
                                '%(threadName)s:%(message)s'))

    with open('config.json') as f:
        config = json.load(f)

    ver = suricata_version()
    if not ver:
        logging.error('unable to communicate with suricata')
        sys.exit(1)
    logging.info('suricata version = %s', ver)
    config.update({'version': ver})

    # TODO Would be good to verify "conf-get outputs.eve-log.enabled"
    #      but this does not seem to be supported by the interface currently

    notifier = Notifier(config)
    parser = EventParser(config, notifier)
    downloader = Downloader(config)

    threads = [notifier, parser, downloader]
    for t in threads:
        t.start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while True:
        stopall = False
        for t in threads:
            if not t.is_alive():
                stopall = True
                break

        if stopall:
            for t in threads:
                if t.is_alive():
                    t.stop = True
                    t.join()
            break

        time.sleep(1)

# vim: set textwidth=80
