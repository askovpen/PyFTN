"""
Message flow from database to links and users
"""

import xml.etree.ElementTree
import io
import traceback
import time
import zipfile
import os

from ftnconfig import suitable_charset, get_link_password, get_link_id, \
	ADDRESS, PACKETTHRESHOLD, BUNDLETHRESHOLD, get_addr_id, get_addr, DOUTBOUND, addrdir, connectdb, \
	EXPORTLOCK, PKTLOCK, BUNDLELOCK, TICLOCK, get_link_pkt_format, get_link_bundler
import ftnimport
import ftn.msg
import ftn.attr
from ftn.ftn import FTNFail, FTNWrongPassword
from stringutil import *
import postgresql.alock

# AUTOCOMMIT assumed

def get_subscribers(db, target, onlyvital=False):
    """ query all subscribers including that who are subscribed to group of the target """

    for r in db.prepare(""" 
    with recursive all_groups(id, level) as
    (
        select $1::BIGINT, 0
      union
        select a.group, g.level+1 from addresses a, all_groups g where a.id = g.id
    )
    select s.subscriber, s.vital, g.level from subscriptions s, all_groups g 
    where s.target = g.id""" + (" and s.vital = True" if onlyvital else ""))(target):

        yield r

def get_addrtree(db, addr):
    for a in db.prepare("""
    with recursive all_nested(id, level) as
    (
        select $1::BIGINT, 0
      union
        select a.id, n.level+1 from addresses a, all_nested n where a.group = n.id
    )
    select id from all_nested where level>0""")(addr):
        yield a





def get_subscriber_messages_n(db, subscriber, domain):
  """ get all subscribed addresses in specified domain and 
      fetch all messages with id>lastsent or if lastsent is None - with processed==0 
      Non-vital netmail subscription should be processed as echomail """
  try:
    query = db.Q_get_subscriber_messages_n
  except AttributeError:
    query = db.Q_get_subscriber_messages_n = db.prepare("""

    with recursive allsubscription(id, target, dir) as 
    (
        select s.id, s.target, 1 from subscriptions s, addresses a 
        where s.subscriber=$1 and s.vital=TRUE and s.target=a.id and a.domain=$2
      Union
        select s.id, a.id, 0 from allsubscription s, addresses a 
        where a.group = s.target
              and (select count(id) from subscriptions where target=a.id) = 0
              and a.domain = $2
    )

    select m.id, m.source, m.destination, m.msgid, m.header, m.body, m.origcharset, m.receivedfrom
    from allsubscription alls, messages m
    where m.processed=0 and m.destination=alls.target
    order by m.id
    ;

  """)

  for m in query(subscriber, domain):
    yield m

def get_direct_messages(db, subscriber):
  try:
    query = db.Q_get_direct_messages
  except AttributeError:
    query = db.Q_get_direct_messages = db.prepare("""

    select m.id, m.source, m.destination, m.msgid, m.header, m.body, m.origcharset, m.receivedfrom
    from messages m
    where m.processed=8 and m.destination=$1
    order by m.id
    ;

  """)

  for m in query(subscriber):
    yield m


# SET cpu_tuple_cost = 0.2; set enable_hashjoin=false; set enable_mergejoin=false;


def get_subscriber_messages_e_heavy(db, subscriber, domain):
  """ get all subscribed addresses in specified domain and 
      fetch all messages with id>lastsent or if lastsent is None - with processed==0 """

  try:
    query = db.Q_get_subscriber_messages_e
  except AttributeError:
    # seems checking that nobody is subscribed to subgroup is not needed for echomail
    # and should be removed
    query = db.Q_get_subscriber_messages_e = db.prepare("""

    with recursive allsubscription(id, lastsent, target, dir) as 
    (
        select id, lastsent, target, 1 from subscriptions where subscriber=$1
      Union
        select s.id, s.lastsent, a.id, 0 from allsubscription s, addresses a 
        where a.group = s.target
    )

    select m.id, m.source, m.destination, m.msgid, m.header, m.body, m.origcharset, m.receivedfrom, alls.id
    from allsubscription alls, addresses sa, messages m
    where sa.id=alls.target and sa.domain=$2 and 
          m.id>alls.lastsent and m.destination=alls.target and
          m.receivedfrom<>$1 and m.processed<>5
    order by m.id
    ;

  """)

  for m in query(subscriber, domain):
    yield m
        #id_msg, src, dest, msgid, header, body, recvfrom, withsubscr

def get_subscriber_messages_e(db, subscriber, domain):
  for target_id, target_name, target_last, subs_id, subs_last in get_subscriptions_x(db, subscriber, domain):
    if target_last>subs_last:
      print ("something new", target_id, target_name, target_last, subs_id, subs_last)
      for mid,msrc,mdst,mmsgid,mhdr,mbody,mchr,mrecvfrom,mproc in db.prepare(
            "select m.id, m.source, m.destination, m.msgid, m.header, "
            "m.body, m.origcharset, m.receivedfrom, m.processed from messages m where destination=$1 and id>$2 order by id")(target_id, subs_last):
        yield mid, msrc, mdst, mmsgid, mhdr, mbody, mchr, mrecvfrom, subs_id, mproc


def _get_messages(db, dest_id, lastsent):
  """ use lastsent >= -1 for fetching echomail
      or lastsent = None for netmail """

  try:
    Q_get_subscription_messages_e = db.Q_get_subscription_messages_e
    Q_get_subscription_messages_n = db.Q_get_subscription_messages_n

  except AttributeError:

    Q_get_subscription_messages_e = db.Q_get_subscription_messages_e = db.prepare(
        "select m.id, s.domain, s.text, m.msgid, m.header, m.body, m.receivedfrom "
        "from messages m, addresses s "
        "where m.id>$2 and m.destination=$1"
        "and m.source=s.id")

    Q_get_subscription_messages_n = db.Q_get_subscription_messages_n = db.prepare(
        "select m.id, s.domain, s.text, m.msgid, m.header, m.body, m.receivedfrom "
        "from messages m, addresses s "
        "where m.destination=$1 and m.processed=0"
        "and m.source=s.id")


  if lastsent is not None:
    # echomail-style 

    for m in Q_get_subscription_messages_e(dest_id, lastsent):
      yield m[0], db.FTN_backdomains[m[1]], m[2], m[3], m[4], m[5], m[6]

  else:
    # netmail-style

    for m in Q_get_subscription_messages_n(dest_id):
      yield m[0], db.FTN_backdomains[m[1]], m[2], m[3], m[4], m[5], m[6]

  return


def update_subscription_watermark(db, subscription, id):
  db.prepare("update subscriptions set lastsent=$1 where id=$2")(id, subscription)



def denormalize_message(orig, dest, msgid, header, body, charset, echodest=None, addvia=None, addseenby=[], addpath=None):
  (origdom, origaddr) = orig
  (destdom, destaddr) = dest

  if charset is None:
    # not imported message
    overwriteCHRS = True
    charset = suitable_charset(None, None, "encode", origdom, origaddr, destdom, destaddr) or 'utf-8'
  else:
    overwriteCHRS = False
  #print(charset)

  if origdom!="node":
    raise FTNFail("message source must be node not %s"%origdom)

  msg=ftn.msg.MSG()

  nltail=len(body)
  while(nltail>0 and body[nltail-1]=="\n"):
    nltail-=1

  try:
    fname=(header.find("sendername").text or '').encode(charset)
    tname=(header.find("recipientname").text or '').encode(charset)
    msg.subj=(unclean_str(header.find("subject").text or '')).encode(charset)
    msg.date=(header.find("date").text or '').encode(charset)
    msg.body = body[:nltail].encode(charset).split(b"\n")
  except UnicodeEncodeError:
    try:
      charset = "cp437"
      fname=(header.find("sendername").text or '').encode(charset)
      tname=(header.find("recipientname").text or '').encode(charset)
      msg.subj=(unclean_str(header.find("subject").text or '')).encode(charset)
      msg.date=(header.find("date").text or '').encode(charset)
      msg.body = body[:nltail].encode(charset).split(b"\n")
    except UnicodeEncodeError:
      charset = "utf-8"
      fname=(header.find("sendername").text or '').encode(charset)
      tname=(header.find("recipientname").text or '').encode(charset)
      msg.subj=(unclean_str(header.find("subject").text or '')).encode(charset)
      msg.date=(header.find("date").text or '').encode(charset)
      msg.body = body[:nltail].encode(charset).split(b"\n")

  #print(fname, tname, subj, date)

  #print(xml.etree.ElementTree.tostring(header, encoding="utf-8").decode("utf-8"))
  ftnheader=header.find("FTN")
#  print(xml.etree.ElementTree.tostring(ftnheader).decode("utf-8"))


  msg.kludge = {}
  for kludge in ftnheader.findall("KLUDGE"):
    #print(kludge.get("name"), kludge.get("value"))
    msg.kludge[kludge.get("name").encode(charset)] = kludge.get("value").encode(charset)

  # overwrite CHRS kludge
  if overwriteCHRS:
    if charset=="fido_relics":
      if b"CHRS:" in msg.kludge:
        del msg.kludge[b"CHRS:"]
    elif charset!="utf-8":
      msg.kludge[b"CHRS:"] = (charset.upper() + " 2").encode("ascii")
    else:
      msg.kludge[b"CHRS:"] = (charset.upper() + " 4").encode("ascii")

#  print(msg.kludge)

  msg.via = [] # netmail only
  for via in ftnheader.findall("VIA"):
    for viak, viav in via.attrib.items():
      msg.via.append((viak.encode(charset), viav.encode(charset)))
  if addvia:
    msg.via.append((b"Via", addvia.encode(charset)))

#  print(msg.via)

  msg.path = []
  msg.seenby = set() # echomail only

  if destdom=="echo":

    dest_zone=ftn.addr.str2addr(echodest)[0]
    my_zone=ftn.addr.str2addr(addpath)[0] # addpath should be this node's address


    for zpth in ftnheader.findall("ZPTH"):
      zpth_record = zpth.get("record")
      msg.add_zpth(zpth_record)

    if my_zone==dest_zone:
      for path in ftnheader.findall("PATH"):
        #print(path.get("record"))
        msg.add_path(path.get("record"))

      if addpath:
        #print("additional path", addpath)
        pathaddr=ftn.addr.str2addr(addpath)
        if not pathaddr[3]:
          msg.add_path(addpath)

    else:
      for path in ftnheader.findall("PATH"):
        pathaddr=ftn.addr.str2addr(path.get("record"))
        msg.add_zpth(ftn.addr.addr2str((my_zone, pathaddr[1], pathaddr[2], None)))

      if addpath:
        #print("additional path", addpath)
        pathaddr=ftn.addr.str2addr(addpath)
        if not pathaddr[3]:
          msg.add_zpth(ftn.addr.addr2str((my_zone, pathaddr[1], pathaddr[2], None)))


    if my_zone==dest_zone:

      for seenby in ftnheader.findall("SEEN-BY"):
        seenbyaddr = (seenby.get("zone"), seenby.get("net"), seenby.get("node"), seenby.get("point"))
        #print(seenbyaddr)
        msg.add_seenby(ftn.addr.addr2str(seenbyaddr))
    else:
      pass # drop old seen-by's

    for seenby in addseenby:
      #print("additional seenby", seenby)
      sbaddr=ftn.addr.str2addr(seenby)
    
      if sbaddr[0]==dest_zone and not sbaddr[3]:
        msg.add_seenby(seenby)


  msg.orig=(fname, ftn.addr.str2addr(origaddr))
  if destdom=="node":
    msg.dest=(tname, ftn.addr.str2addr(destaddr))
    msg.area=None
  elif destdom=="echo":
    #print("packing echomail msg to "+echodest)
    msg.dest=(tname, ftn.addr.str2addr(echodest))
    msg.area=destaddr.encode(charset)
  else:
    raise FTNFail("do not know how to pack message to "+destdom)


  msg.attr=0
  if destdom=="node":
    attrs=[]
    for attr in ftnheader.findall("ATTR"):
      attrs.append(attr.get("id"))
    try:
      msg.attr = ftn.attr.text_to_binary(attrs)
    except:
      traceback.print_exc()

  msg.cost=0
  msg.readcount=0
  msg.replyto=0
  msg.nextreply=0

  #print(msg.__dict__)

  return msg, charset


# ---


class filecommitter:
  def __init__(self, filename):
    self.filename = filename

  def show(self):
    print ("file committer:", self.filename)

  def commit(self):
    print ("commit file", repr(self.filename))
    os.unlink(self.filename)


class netmailcommitter:
  def __init__(self, newstatus=2):
    self.newstatus=newstatus
    self.msglist=set()
    self.msgarqlist=[]
    self.db = connectdb()

  def show(self):
    print ("netmail committer:", self.newstatus, self.msglist)

  def add(self, d):
    if type(d) is netmailcommitter:
      if self.msglist.intersection(d.msglist):
        raise Exception("double export of netmail message")
      self.msglist.update(d.msglist)
      self.msgarqlist.extend(d.msgarqlist)
    else:
      if d[0] in self.msglist:
        raise Exception("double export of netmail message")
      self.msglist.add(d[0])
      if d[1]:
        self.msgarqlist.append(d[1])

  def commit(self):
    try:
     with ftnimport.session(self.db) as sess:
      for addr, name, deliverto, msg, charset in self.msgarqlist:
        print("send audit request to", addr)
        sess.send_message("Audit tracker", addr, name, None, "Audit tracking response", """
This reply confirms that your message has been successfully delivered 
to node %s

*******************************************************************************
%s
*******************************************************************************
"""%(deliverto, msg.as_str(shorten=True).decode(charset)))

    except:
      print("error sending ARq reply")
      traceback.print_exc()

    for msg in self.msglist:
      self.db.prepare("update messages set processed=$2 where id=$1")(msg,self.newstatus)
      print("commit msg #%d"%msg)

    self.msglist=set()
    self.msgarqlist=[]


class echomailcommitter:
  def __init__(self):
    self.lasts = {} # subscription: lastsent
    self.db = connectdb()

  def add_one(self, k, v):
    if k in self.lasts and v<=self.lasts[k]:
      raise Exception("non-monotonic echomail export")
    self.lasts[k] = v

  def add(self, d):
    if type(d) is echomailcommitter:
      for k, v in d.lasts.items():
        self.add_one(k, v)
    else:
      self.add_one(*d)

  def commit(self):
    for k, v in self.lasts.items():
      self.db.prepare("update subscriptions set lastsent=$1 where id=$2")(v, k)
      print("commit subscription %d up to message #%d"%(k, v))
    self.lasts = {}

  def show(self):
    print("echomail committer:", self.lasts)

# --- file export ---

def get_pkt_n(db, link_id):
    with postgresql.alock.ExclusiveLock(db, ((PKTLOCK, link_id))):
      r = db.prepare("select pktn from links where id=$1").first(link_id)
      db.prepare("update links set pktn=pktn+1 where id=$1")(link_id)
    return r

def get_bundle_n(db, link_id):
    with postgresql.alock.ExclusiveLock(db, ((BUNDLELOCK, link_id))):
      r = db.prepare("select bundlen from links where id=$1").first(link_id)
      db.prepare("update links set bundlen=bundlen+1 where id=$1")(link_id)
    return r

def get_tic_n(db, link_id):
    with postgresql.alock.ExclusiveLock(db, ((TICLOCK, link_id))):
      r = db.prepare("select ticn from links where id=$1").first(link_id)
      db.prepare("update links set ticn=ticn+1 where id=$1")(link_id)
    return r


def file_export(db, address, password, what):
  """ This generator fetches messages from database and
      yields objects, that contain the file information
      and instructions how to commit to db inforamtion
      about successful message delivery """

    # first netmail
    # then requested file
    # then echoes
    # then filebox
    # and at last fileechoes

  print("export to", repr(address), repr(password), repr(what))

  if password != (get_link_password(db, address) or ""):
      raise FTNWrongPassword()

  print("password is correct" if password else "password is empty")


  # WARNING!
  # unprotected sessions never must do queries as it may result in leaking netmail
  # if address of some hub is spoofed

  addr_id = get_addr_id(db, db.FTN_domains["node"], address)
  link_pkt_format = get_link_pkt_format(db, address)
  link_bundler = get_link_bundler(db, address)


  if password and ("netmail" in what):
    explock = postgresql.alock.ExclusiveLock(db, ((EXPORTLOCK["netmail"], addr_id)))
    if explock.acquire(False):
      try:

        print ("exporting netmail")
    # only vital subscriptions is processed
    # non-vital (CC) should be processed just like echomail

    # set password in netmail packets
        p = pktpacker(link_pkt_format, ADDRESS, address, get_link_password(db, address) or '', lambda: get_pkt_n(db, get_link_id(db, address)), lambda: netmailcommitter())

    #..firstly send pkts in outbound
        for id_msg, src, dest, msgid, header, body, origcharset, recvfrom in get_subscriber_messages_n(db, addr_id, db.FTN_domains["node"]):

          print("netmail %d recvfrom %d pack to %s"%(id_msg, recvfrom, repr(address)))

      # if exporting to utf8z always use UTF-8
          if link_pkt_format == "utf8z":
            origcharset = "utf-8"

          myvia = "PyFTN " + ADDRESS + " " + time.asctime()
          srca=db.prepare("select domain, text from addresses where id=$1").first(src)
          dsta=db.prepare("select domain, text from addresses where id=$1").first(dest)

          try:
            msg, msgcharset = denormalize_message(
            (db.FTN_backdomains[srca[0]], srca[1]),
            (db.FTN_backdomains[dsta[0]], dsta[1]),
            msgid, header, body, origcharset, address, addvia = myvia)
          except:
            raise Exception("denormalization error on message id=%d"%id_msg+"\n"+traceback.format_exc())

          try:
            print ("export msg attributes", msg.attr)
          except:
            traceback.print_exception()

          if 'AuditRequest' in ftn.attr.binary_to_text(msg.attr):
            audit_reply = (db.FTN_backdomains[srca[0]], srca[1]), header.find("sendername").text, address, msg, msgcharset
          else:
            audit_reply = None

          for x in p.add_item(msg, (id_msg, audit_reply)): # add ARQ flag
            yield x

        for x in p.flush():
          yield x

        del p

      finally:
        explock.release()
    else:
      print ("could not acquire netmail lock")

  if "direct" in what: # available for unprotected sessions
    # export messages with processed==8 and destination==addr_id
   explock = postgresql.alock.ExclusiveLock(db, ((EXPORTLOCK["netmail"], addr_id)))
   if explock.acquire(False):

    print ("exporting direct netmail")
    # only vital subscriptions is processed
    # non-vital (CC) should be processed just like echomail

    # set password in netmail packets
    link_id=get_link_id(db, address, withfailback=True)
    p = pktpacker(link_pkt_format, ADDRESS, address, get_link_password(db, address) or '', lambda: get_pkt_n(db, link_id), lambda: netmailcommitter(newstatus=7))

    #..firstly send pkts in outbound
    for id_msg, src, dest, msgid, header, body, origcharset, recvfrom in get_direct_messages(db, addr_id):

      print("direct netmail %d recvfrom %d pack to %s"%(id_msg, recvfrom, repr(address)))

      # if exporting to utf8z always use UTF-8
      if link_pkt_format == "utf8z":
        origcharset = "utf-8"

      myvia = "PyFTN " + ADDRESS + " DIRECT " + time.asctime()
      srca=db.prepare("select domain, text from addresses where id=$1").first(src)
      dsta=db.prepare("select domain, text from addresses where id=$1").first(dest)

      try:
        msg, msgcharset = denormalize_message(
            (db.FTN_backdomains[srca[0]], srca[1]),
            (db.FTN_backdomains[dsta[0]], dsta[1]), 
            msgid, header, body, origcharset, address, addvia = myvia)
      except:
        raise Exception("denormalization error on message id=%d"%id_msg+"\n"+traceback.format_exc())

      try:
        print ("export msg attributes", msg.attr)
      except:
        traceback.print_exception()

      if 'AuditRequest' in ftn.attr.binary_to_text(msg.attr):
        audit_reply = (db.FTN_backdomains[srca[0]], srca[1]), header.find("sendername").text, address, msg, msgcharset
      else:
        audit_reply = None

      for x in p.add_item(msg, (id_msg, audit_reply)): # add ARQ flag
        yield x

    for x in p.flush():
      yield x

    del p

    explock.release()
    pass

  if password and ("echomail" in what):
    explock = postgresql.alock.ExclusiveLock(db, ((EXPORTLOCK["echomail"], addr_id)))
    if explock.acquire(False):
      try:
        print ("exporting echomail")
        #..firstly send bundles in outbound

        #

        if link_bundler:
          p = pktpacker(link_pkt_format, ADDRESS, address, get_link_password(db, address) or '', lambda: get_pkt_n(db, get_link_id(db, address)), lambda: echomailcommitter(),
            bundlepacker(link_bundler, address, lambda: get_bundle_n(db, get_link_id(db, address)), lambda: echomailcommitter()))
        else:
          p = pktpacker(link_pkt_format, ADDRESS, address, get_link_password(db, address) or '', lambda: get_pkt_n(db, get_link_id(db, address)), lambda: echomailcommitter())

        subscache = {}
        for id_msg, xxsrc, dest, msgid, header, body, origcharset, recvfrom, withsubscr, processed in get_subscriber_messages_e(db, addr_id, db.FTN_domains["echo"]):

          will_export = True # do we really must send message or just update last_sent pointer

          #print("echomail %d"%id_msg, repr(dest))
          #print("dest %d recvfrom %s subscr %s pack to %s"%(dest, repr(recvfrom), repr(withsubscr), address))
          # ignore src - for echomail it is just recv_from

          # if exporting to utf8z always use UTF-8
          if link_pkt_format == "utf8z":
            origcharset = "utf-8"

          if recvfrom == addr_id:
            #print ("Message from this link, will not export")
            will_export = False

          if processed == 5:
            #print ("Archived message, will not export")
            will_export = False

          # check commuter
          subscriber_comm = db.FTN_commuter.get(withsubscr)
          if subscriber_comm is not None: # must check as None==None => no export at all
            # get subscription through what message was received
            recvfrom_subscription = db.prepare("select id from subscriptions where target=$1 and subscriber=$2").first(sub_tart, m_recvfrom)
            recvfrom_comm = db.FTN_commuter.get(recvfrom_subscription)
            if recvfrom_comm == subscriber_comm:
              print("commuter %d - %d, will not export"%(withsubscr, recvfrom_subscription))
              will_export = False
    #          continue # do not forward between subscriptions in one commuter group (e.g. two uplinks)

          if dest in subscache:
            subscribers = subscache[dest]
          else:
            subscribers = db.prepare("select a.domain, a.text from subscriptions s, addresses a where s.target=$1 and s.subscriber=a.id")(dest)

            if not all([x[0]==db.FTN_domains["node"] for x in subscribers]):
              raise FTNFail("subscribers from wrong domain for "+str(sub_targ))

            #    print(sub_id, sub_targ, "all subscribers:", [x[1] for x in subscribers])

            subscribers = subscache[dest] = [x[1] for x in subscribers]

          #print("subscribers:", repr(subscribers))

    #      if withsubscr not in subscribers:
    #        raise Exception("strange: exporting to non-existent subscription", withsubscr)

          dsta = db.prepare("select domain, text from addresses where id=$1").first(dest)

          # modify path and seen-by
          # seen-by's - get list of all subscribers of this target; add subscribers list
          #... if go to another zone remove path and seen-by's and only add seen-by's of that zone -> ftnexport

          if will_export: # create MSG else do not bother
           try:
            msg, msgcharset = denormalize_message( 
                ("node", ADDRESS),
                (db.FTN_backdomains[dsta[0]], dsta[1]), 
                msgid, header, body, origcharset, address, addseenby=subscribers, addpath=ADDRESS)
           except:
            raise Exception("denormalization error on message id=%d"%id_msg+"\n"+traceback.format_exc())

          for x in p.add_item((msg if will_export else None), (withsubscr, id_msg)):
            yield x

        for x in p.flush():
          yield x

      finally:
        explock.release()
    else:
      print("could not acquire echomail lock")

  if password and ("filebox" in what):
   explock = postgresql.alock.ExclusiveLock(db, ((EXPORTLOCK["filebox"], addr_id)))
   if explock.acquire(False):
    # ..send freq filebox
    print ("exporting filebox")
    dsend = addrdir(DOUTBOUND, address)
    if os.path.isdir(dsend):
      print ("exporting daemon outbound")
      for f in os.listdir(dsend):
        fname = os.path.join(dsend, f)
        if os.path.isfile(fname):
          obj = outfile()
          obj.data = open(fname, "rb")
          obj.filename = f
          obj.length = os.path.getsize(fname)
          yield obj, filecommitter(fname)

    explock.release()


  if password and ("fileecho" in what):
   explock = postgresql.alock.ExclusiveLock(db, ((EXPORTLOCK["fileecho"], addr_id)))
   if explock.acquire(False):
    # ..send fileechoes
    print ("exporting fileechoes (nothing here)")
    pass #1/0
    explock.release()

  return


class outfile:
  def commit(self):
    self.commitdb()
  # filename
  # data
  # length
  def show(self):
    print ("exported file",self.filename,self.length)

class pktpacker:
  def __init__(self, format, me, node, passw, counter, commitgen, packto=None):
    self.format = format
    self.packet = None
    self.node = node
    self.me = me
    self.packto = packto
    self.counter = counter
    self.commitgen = commitgen
    self.passw = passw.encode("utf-8")[:8]
    self.committer = None

  def add_item(self, m, commitdata):
    if m is not None:
      if self.packet is None:
        self.packet=ftn.pkt.PKT()
        self.packet.password=self.passw
        self.packet.source=ftn.addr.str2addr(self.me)
        self.packet.destination=ftn.addr.str2addr(self.node)
        self.packet.date=time.localtime()
        self.packet.msg=[]
        self.packet.approxlen=0

      self.packet.msg.append(m)
      self.packet.approxlen+=len(m.pack()) # double packing :(

    if self.committer is None:
      self.committer = self.commitgen()

    # if m is not None it will be send else just last_sent will be updated
    self.committer.add(commitdata)

    if self.packet and self.packet.approxlen>PACKETTHRESHOLD:
      for x in self.pack():
        yield x

  def pack(self):
    if self.packet is not None:
      p = outfile()
      if self.format=='utf8z':
        p.filename = "%08x.upkt"%self.counter()
      else:
        p.filename = "%08x.pkt"%self.counter()

      print("PACKET %s"%p.filename)
      p.data = io.BytesIO()
      self.packet.save(p.data, format=self.format)
      p.length = p.data.tell()
      p.data.seek(0)
      self.packet = None
    else:
      print("PACKET = None")
      print("Committer:")
      print(self.committer.show() if self.committer else "None")
      p = None

    if p or self.committer:
      if self.packto:
        for x in self.packto.add_item(p, self.committer):
          yield x
      else:
        yield p, self.committer

    self.committer = None

  def flush(self):
    for x in self.pack():
      yield x
    if self.packto:
      for x in self.packto.flush():
        yield x


class bundlepacker:
  def __init__(self, bundler, destination, counter, commitgen, packto=None):
    self.bundler = bundler
    self.destination = destination
    self.counter = counter
    self.bundle = None
    self.commitgen = commitgen
    self.packto = packto
    self.committer = None

  def add_item(self, p, commitdata):
    if p is not None:
      if self.bundle is None:
        fo = io.BytesIO()
        self.bundle = (fo, zipfile.ZipFile(fo, "w", zipfile.ZIP_DEFLATED))

      self.bundle[1].writestr(p.filename, p.data.read())

    if self.committer is None:
      self.committer = self.commitgen()

    self.committer.add(commitdata)

    if self.bundle and self.bundle[0].tell()>BUNDLETHRESHOLD:
      for x in self.pack():
        yield x

  def pack(self):
    if self.bundle is not None:
      b = outfile()
      zone, net, node, point = ftn.addr.str2addr(ADDRESS)
      dzone, dnet, dnode, dpoint = ftn.addr.str2addr(self.destination)
      if dpoint:
          basename = "0000p%03x"%dpoint
      else:
          netd = net - dnet
          noded = node - dnode
          basename = "%04x%04x"%(netd%0x10000, noded%0x10000)
      c = self.counter()
      d = c%16
      c = c//16
      e = ["mo", "tu", "we", "th", "fr", "sa", "su"][c%7]
      c = c//7
      b.filename = "%s.%s%x"%(basename, e, d)
      print("BUNDLE %s"%b.filename)
      self.bundle[1].close()
      b.data = self.bundle[0]
      b.length = b.data.tell()
      b.data.seek(0)
      self.bundle = None
    else:
      print("BUNDLE = None")
      print("Committer:")
      print(self.committer.show() if self.committer else "None")
      b = None

    if b or self.committer:
      if self.packto:
        for x in self.packto.add_item(b, self.committer):
          yield x
      else:
        yield b, self.committer

    self.committer = None

  def flush(self):
    for x in self.pack():
      yield x
    if self.packto:
      for x in self.packto.flush():
        yield x

# --------------- auxiliary functions ---------------------

def get_node_subscriptions(db, subscriber, targetdomain, asuplink = False):
    return [x[0] for x in db.prepare("select t.text, s.id, s.lastsent from subscriptions s, addresses sr, addresses t "
        "where s.target=t.id and t.domain=$1 and s.subscriber=sr.id and sr.domain=$2 and sr.text=$3"
        + (" and s.vital = True" if asuplink else "") + " order by t.text")(db.FTN_domains[targetdomain], db.FTN_domains["node"], subscriber)]

def get_subscriptions_x(db, subscriber_id, targetdomain_id, asuplink = False):
    return [x for x in db.prepare("select t.id, t.text, t.last, s.id, s.lastsent from subscriptions s, addresses t "
        "where s.target=t.id and t.domain=$1 and s.subscriber=$2 and t.last is not NULL"
        + (" and s.vital = True" if asuplink else "") + " order by t.text")(targetdomain_id, subscriber_id)]


def get_all_targets(db, targetdomain):
    return [x[0] for x in db.prepare("select t.text from addresses t where t.domain=$1 order by text")(db.FTN_domains[targetdomain])]

# --- NNTP ---

def nntp_list_active(db, newerthan=None):
  group_q = db.prepare("select min(id), max(id) from messages where destination=$1")

  if newerthan is None:
    query = db.prepare("select t.id, t.text from addresses t "
        "where t.domain=$1 order by text")(db.FTN_domains["echo"])
  else:
    query = db.prepare("select t.id, t.text from addresses t "
        "where t.domain=$1 and created>=$2 order by text")(db.FTN_domains["echo"], newerthan)

  for aid, atext in query:
    low, high = group_q.first(aid) # new message always added at end; messages cannot be hidden
    yield atext, low, high

def nntp_group(db, group):
  aid=db.prepare("select id from addresses where text=$1").first(group)
  if aid is None:
    return None
  return db.prepare("select count(id), min(id), max(id) from messages where destination=$1").first(aid)

def nntp_group_list(db, group, gte=None, lte=None):
  aid=db.prepare("select id from addresses where text=$1").first(group)
  if aid is None:
    return None
  criteria=""
  if gte:
    criteria+=" and id>=%d"%gte
  if lte:
    criteria+=" and id<=%d"%lte
  for x in db.prepare("select id from messages where destination=$1"+criteria+" order by id")(aid):
    yield x[0]

def nntp_next(db, group, msgn):
  aid=db.prepare("select id from addresses where text=$1").first(group)
  if aid is None:
    return None
  r = db.prepare("select id from messages where destination=$1 and id>$2"
        "order by id asc limit 1 offset 0").first(aid, msgn)
  if r is None:
    return None
  return r[0]

def nntp_prev(db, group, msgn):
  aid=db.prepare("select id from addresses where text=$1").first(group)
  if aid is None:
    return None
  r = db.prepare("select id from messages where destination=$1 and id<$2"
        "order by id desc limit 1 offset 0").first(aid, msgn)
  if r is None:
    return None
  return r[0]

def nntp_fetch(db, head, body, msgid=None, group=None, article=None):
  return None

def nntp_msgid(db, group, msgn):
  pass

# --- **** ---

def count_messages_to(db, address):
    return int(db.prepare("select count(*) from messages where destination=$1")(address)[0][0])

def get_matching_targets(db, targetdomain, mask):
    mask = mask.replace("+", "++").replace("%", "+%").replace("_", "+_").replace("*", "%").replace("?", "_")
    return [x[0] for x in db.prepare("select t.text from addresses t where t.domain=$1 and t.text like $2 escape '+'")\
        (db.FTN_domains[targetdomain], mask)]

def get_subnodes(db, addr):
  return [x[0] for x in db.prepare("select s.text from addresses n, addresses s where s.group=n.id and n.text=$1 and n.domain=$2")(addr, db.FTN_domains["node"])]

def get_supernode(db, addr):
  r = db.prepare("select s.text from addresses n, addresses s where s.id=n.group and n.text=$1 and n.domain=$2")(addr, db.FTN_domains["node"])
  if len(r):
    return r[0][0]
  else:
    return None

if __name__ == "__main__":
    from ftnconfig import connectdb
#    for s in get_node_subscriptions(connectdb(), "2:5020/4441", "echo"):
#        if s.find("TEST")!=-1:
#            print (s)

    print (get_subnodes(connectdb(), '2:5020/733315'))

