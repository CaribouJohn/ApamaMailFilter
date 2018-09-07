'''
Created on 23 Aug 2018

@author: JHEA
'''
from apama.eplplugin import EPLAction, EPLPluginBase, Correlator, Event
from imapclient import IMAPClient
import json
import time
import sys
import threading
from msilib.schema import CreateFolder

def poll(plugin,interval):
    """
    This method runs on a separate thread allowing Apama to update .

    @param interval:    The interval at which the thread should poll the directory for changes.
    @param channel:        The EPL channel which will receive Changes events.
    """
    while (True):
        try:
            # check the current contents against self.OrigSeqFiles
            plugin.getLogger().info("MailFilterPlugin Thread triggering" + str(interval))
            evt = Event('TriggerRefresh',{})
            Correlator.sendTo("mail_events", evt)
        except:
            plugin.getLogger().error("Poll Thread Exception: %s", sys.exc_info()[1])

        time.sleep(interval)
        
class MailFilterPlugin(EPLPluginBase):
    '''
    classdocs
    '''
    
    # Initialisation is simply a a case of reading in the config
    # and  
    #
    def __init__(self, params):
        '''
        Constructor
        '''
        super(MailFilterPlugin,self).__init__(params)
        self.hostdetails = self.loadJSON('hostdetails.json')
        self.simulate = self.hostdetails['parameters']['simulate']
        self.moveToFolderThreshold = self.hostdetails['parameters']['moveToFolderThreshold']
        self.updateFrequency = self.hostdetails['parameters']['updateFrequency']

        
        self.config = self.loadJSON('config.json')
        self.mailhost = self.config['mail']['host']
        self.mailport = self.config['mail']['port']
        self.mailuser = self.config['mail']['user']
        self.mailpassword = self.config['mail']['password']
         
        self.getLogger().info("MailFilterPlugin initialised {h} user = {u} ".format(h=self.mailhost,u=self.mailuser))
        self.getLogger().info("Config = {c} ".format(c=self.config))
        
        #make sure we are logged in
        try: 
            self.client = IMAPClient(host=self.mailhost,port=self.mailport,ssl=False)
            self.client.login(self.mailuser, self.mailpassword)
        except Exception as err:
            self.getLogger().info("MailFilterPlugin Failed to log in : "+str(err))
    
        self.monitorMail()
    
    #use json format for config and dynamic data    
    def loadJSON(self,fileName):
        with open(fileName,'r') as f:
            return json.load(f)
                
    def saveJSON(self,userObj,fileName):
        with open(fileName,'w') as f:
            json.dump(userObj,f)

    @EPLAction("action< > returns dictionary<string,MailSummary> ")
    def generateSummary(self):
        #
        #this method will update the whole structure from last_seen to the last message
        #        
        try:
            select_dict_bytes = self.client.select_folder('INBOX')
            max_uid = select_dict_bytes.get('UIDNEXT'.encode())
            last_seen = self.hostdetails['lastmessage']
            if last_seen < max_uid:                
                fetch_str = str(last_seen)+':'+str(max_uid)
                self.getLogger().info("Retrieving : "+ fetch_str)
                response = self.client.fetch(fetch_str, ['FLAGS', 'ENVELOPE'])
                for message_id, data in response.items():
                    envelope=data[b'ENVELOPE']
                    
                    #this can be a list
                    for s in envelope.sender:
                        decodedHost = s.host.decode()
                        if str(decodedHost) not in self.hostdetails['sourcehosts']:
                            #N.B. if the same source uses multiple names this will show the first encountered
                            self.hostdetails['sourcehosts'][decodedHost] = {'hostname':decodedHost,'occurrences':1,'moveToFolder':False,'folder':""}
                            #print ("Adding {name} {host}".format(name=s.name, host=s.host))
                        else:
                            self.hostdetails['sourcehosts'][decodedHost]['occurrences'] += 1
                            
                        #process the actual message in apama
                        evt = Event('MailUpdate',{"msgid":message_id,"hostname":decodedHost})
                        Correlator.sendTo("mail_events", evt)
                            
                self.hostdetails['lastmessage'] = max_uid;
                self.saveJSON(self.hostdetails, 'hostdetails.json')
            
            return self.hostdetails['sourcehosts']
        
        except Exception as err:
            self.getLogger().info("MailFilterPlugin Failed to get a summary : "+str(err))
        
        #empty or the current configuration 
        return self.config['sourcehosts']
    
    def scanMailbox(self,last_seen):
        try:
            select_dict_bytes = self.client.select_folder('INBOX')
            max_uid = select_dict_bytes.get('UIDNEXT'.encode())
            fetch_str = str(last_seen)+':'+str(max_uid)
            self.getLogger().info("Scanning : "+ fetch_str)
            response = self.client.fetch(fetch_str, ['FLAGS', 'ENVELOPE'])
            for message_id, data in response.items():
                envelope=data[b'ENVELOPE']
                
                #this can be a list
                for s in envelope.sender:
                    #process the actual message in apama
                    decodedHost = s.host.decode()
                    evt = Event('MailUpdate',{"msgid":message_id,"hostname":decodedHost})
                    Correlator.sendTo("mail_events", evt)

        except Exception as err:
            self.getLogger().info("MailFilterPlugin Failed to scan : "+str(err))
        
    
    def createFolder(self, destination):
        if( not self.simulate and not self.client.folder_exists(destination) ):
            self.getLogger().error("Created Folder : {} ".format(destination))
        else:
            self.getLogger().error("Simulated Created Folder : {} ".format(destination))
        return True;
    
    @EPLAction("action< integer, string >")
    def moveMessage(self, msgid , destination):
        if( not self.simulate ):
            #it might have been deleted...
            if( not self.client.folder_exists(destination) ):
                self.createFolder(destination)
            
            #atomic move - older servers may not support this.
            self.client.move(msgid,destination)                
            self.getLogger().error("Moved Message : {} from {}".format(msgid,destination))
        else:
            self.getLogger().error("Simulated Moved Message : {} from {}".format(msgid,destination))
    
    #The lazy connection that triggers behaviour 
    @EPLAction("action<> returns boolean")
    def monitorMail(self):
        try:
            self.thread = threading.Thread(target=poll, args=(self,30), name='Apama Mail Filter polling thread')
            self.thread.start()
            self.getLogger().info("MailFilterPlugin Thread started")
            return True
        except:
            self.getLogger().error("Failed to start monitor polling thread : %s", sys.exc_info()[1])
            return False

    #This toggles the moveToFolder flag 
    @EPLAction("action< UpdateRule >")
    def updateHost(self, ruleUpdate):
        self.getLogger().info("UpdateRule received in Apama as an eplplugin.Event instance: " + str(ruleUpdate) )
        if ruleUpdate.fields['enabled']:
            self.hostdetails['sourcehosts'][ruleUpdate.fields['hostname']]['moveToFolder'] = True
        else:
            self.hostdetails['sourcehosts'][ruleUpdate.fields['hostname']]['moveToFolder'] = False
        self.saveJSON(self.hostdetails, 'hostdetails.json')
        #move all messages for rule
        self.scanMailbox(1)
    