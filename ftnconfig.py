
import postgresql
import re

db=postgresql.open("pq://fido:bgdis47wb@192.168.0.3/fido")

DUPDIR="dupmsg"
BADDIR="badmsg"
SECDIR="secmsg"
ADDRESS="2:5020/12000"
INBOUND="/tank/home/sergey/fido/fidotmp/archive"
OUTBOUND="/tank/home/sergey/fido/send"

# - values configured in database

def init_domains(db):
  try:
    return db.FTN_domains, db.FTN_backdomains
  except AttributeError:
    db.FTN_domains = {}
    db.FTN_backdomains = {}
    for domain_id, domain_name in db.prepare("SELECT id, name FROM domains"):
      if domain_name=="fidonet node":
        db.FTN_domains["node"]=domain_id
        db.FTN_backdomains[domain_id]="node"
      elif domain_name=="fidonet echo":
        db.FTN_domains["echo"]=domain_id
        db.FTN_backdomains[domain_id]="echo"
      elif domain_name=="fidonet fileecho":
        db.FTN_domains["fileecho"]=domain_id
        db.FTN_backdomains[domain_id]="fileecho"
      elif domain_name=="fidonet backbone":
        db.FTN_domains["backbone"]=domain_id
        db.FTN_backdomains[domain_id]="backbone"
    return db.FTN_domains, db.FTN_backdomains

init_domains(db)

# -

RE_russian=re.compile("RU\.|SU\.|MO\.|R50\.|N50|HUMOR\.|TABEPHA$|XSU\.|ESTAR\.|FLUID\.|SURVIVAL\.GUIDE$|STARPER\.|VGA\.PLANETS|OBEC\.|SPB\.|"
            "CTPAHHOE\.MECTO|PVT\.|1072\.|ADINF\.|ZX\.SPECTRUM|\$CRACK\$|BOCHAROFF\.|ZONE7|MISTRESS\.|FAR\.SUPPORT|FIDONET\.HISTORY|MINDO\.|"
            "TYT\.BCE\.HACPEM|PUSHKIN\.|715\.|XPEHOBO\.MHE|CKA3KA\.CTPAHHOE\.MECTO|UZ\.|FREUD|GUITAR\.SONGS|RUSSIAN\.|LOTSMAN\.|AVP\.|1200\.|"
            "MU\.|REAL\.SIBERIAN\.VALENOK|KAZAN\.|BRAKE\'S\.MAILER\.SUPPORT|\$HACKING\$|GERMAN\.RUS|GSS\.PARTOSS|NODEX\.|380\.|SMR\.|"
            "TESTING$|ESPERANTO\.RUS|RUS\.|BEL\.|MOLDOVA\.|UKR\.|UA\.|RUS_|RUSSIAN_|KAK\.CAM-TO|DEMO.DESIGN|FTNED\.RUS|REAL\.SPECCY|"
            "TAM\.HAC\.HET|T-MAIL|1641\.|DN\.|TVER\.|ASCII_ART|GER\.RUS|KHARKOV\.|XCLUDE\.|CB\.RADIO")

RE_latin=re.compile("IC$|ENET\.|FN_|FTSC_|PASCAL|BLUEWAVE")

RE_ukrainian=re.compile("ZZUKR\.|ZZUA\.")


def suitable_charset(chrs_kludge, mode, srcdom, srcaddr, destdom, destaddr): # mode="encode"|"decode"

    if chrs_kludge=="CP866 2": # can trust
      charset="cp866"

#      charset=msg.kludge[b"CHRS:"].rsplit(b" ", 1)[0].decode("ascii")
      #if charset in set(("IBMPC", "+7 FIDO", "ALT", "+7_FIDO", "CP-866", 
      #      "RUSSIAN", "RUS_FTN", "FIDO", "UKR", "R_FIDO", "TABLE", "IBMPC2", "R FIDO", 
      #      "FTN", "JK", "KOI", "CP808", "RUFIDO", "Russian", "IBM", "+7FIDO")):
      #    charset="cp866"

    if destdom=="echo":
      #print("area for charset [%s]"%repr(msg.area))
      if RE_russian.match(destaddr):
        charset="cp866"
      elif RE_latin.match(destaddr):
        charset="latin-1"
      elif RE_ukrainian.match(destaddr):
        charset="cp1125"

    else:
      # netmail default charset
      #if destaddr.startswith("2:50")
      charset="cp866"
