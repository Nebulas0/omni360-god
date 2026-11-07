// Foundry's main Store: the original cover-grid, featured and game-page renderers.
#include "Foundry.h"
#include "FoundryProtocol.h"
#include "GameListUI.h"
#include "CoverArt.h"
#include "StoreArt.h"
#include "Keyboard.h"
#include "cJSON.h"
#include <xtl.h>
#include <string>
#include <vector>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

namespace {
struct Title { std::string id,name,kind,genre;unsigned long art;char letter; };
struct Release { std::string id,name,size; };
struct Row { std::string name;std::vector<Title> titles;std::vector<StoreTileView> tiles;int scroll;Row():scroll(0){} };
static const char *letters="*#ABCDEFGHIJKLMNOPQRSTUVWXYZ";
static std::string Text(const cJSON *o,const char *key){const cJSON *v=cJSON_GetObjectItemCaseSensitive(o,key);return cJSON_IsString(v)&&v->valuestring?v->valuestring:"";}
static std::string Encode(const std::string &s){static const char hex[]="0123456789ABCDEF";std::string out;for(size_t i=0;i<s.size();++i){unsigned char c=s[i];if((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||c=='-'||c=='_')out+=c;else{out+='%';out+=hex[c>>4];out+=hex[c&15];}}return out;}
static Title ReadTitle(const cJSON *o){Title t;t.id=Text(o,"group_id");t.name=Text(o,"name");t.kind=Text(o,"kind");t.genre=Text(o,"genre");std::string id=Text(o,"art_title_id"),letter=Text(o,"letter");t.art=FoundryHex(id,8)?strtoul(id.c_str(),NULL,16):0;t.letter=letter.size()==1?letter[0]:'#';return t;}
static std::vector<Title> Titles(const cJSON *array){std::vector<Title> out;for(int i=0;cJSON_IsArray(array)&&i<cJSON_GetArraySize(array)&&i<48;++i){Title t=ReadTitle(cJSON_GetArrayItem(array,i));if(FoundryHex(t.id,32)&&!t.name.empty()&&t.name.size()<=512)out.push_back(t);}return out;}
static void Tiles(const std::vector<Title> &titles,std::vector<StoreTileView> *out){out->resize(titles.size());for(size_t i=0;i<titles.size();++i){StoreTileView &v=(*out)[i];v.titleId=titles[i].art;v.name=titles[i].name.c_str();v.letter=titles[i].letter;v.indie=false;}}
static void Pump(){PumpCoverArt();PumpStoreArt();}
static bool Active(){QueueJobSnapshot s;return SnapshotFoundryJobs(&s,1)&&s.state!=QUEUE_FINISHED;}
static bool Search(std::string *query){std::string typed;if(OpenKeyboardToString(0,&typed,L"Search Foundry",L"Search your server's games and add-ons",L"")!=ERROR_SUCCESS)return false;*query=typed;ResyncUiInput();return true;}
static bool Browse(const char *games,const char *content,const std::string &kind,char letter,const std::string &query,const std::string &forTitle=std::string(),const std::string &parent=std::string());

static bool LoadReleases(const Title &title,const std::string &cursor,std::vector<Release> *releases,std::string *next,std::string *previous)
{
    cJSON *reply=NULL;if(!FoundryStoreRequest("/store/titles/"+title.id+"?cursor="+(cursor.empty()?"0":cursor),&reply))return false;
    releases->clear();const cJSON *rows=cJSON_GetObjectItemCaseSensitive(reply,"releases");
    for(int i=0;cJSON_IsArray(rows)&&i<cJSON_GetArraySize(rows)&&i<50;++i){const cJSON *o=cJSON_GetArrayItem(rows,i);Release r;r.id=Text(o,"release_id");r.name=Text(o,"name");std::string n=Text(o,"source_bytes");unsigned long long bytes=0;FoundryDecimal(n.c_str(),&bytes);char size[48];_snprintf(size,sizeof(size),"%.2f GB",bytes/1073741824.0);size[sizeof(size)-1]=0;r.size=size;if(r.id.compare(0,8,"catalog-")==0&&r.name.size()<=1024)releases->push_back(r);}
    *next=Text(reply,"next_cursor");*previous=Text(reply,"previous_cursor");cJSON_Delete(reply);return true;
}
static bool Game(const Title &title,const char *games,const char *content,const std::string &forTitle=std::string())
{
    std::vector<Release> releases;std::string next,previous,cursor;
    if(!LoadReleases(title,cursor,&releases,&next,&previous))return false;
    if(releases.empty()){ShowMessageUI("Foundry","This title has no available sources. Refresh the Store.",NULL);return false;}
    if(title.art){RequestStoreDetails(title.art);RequestStoreArt(title.art,STORE_ART_BACKGROUND);RequestStoreCoverArt(&title.art,1);}
    int chosen=0,versionFocus=0,versionScroll=0,focus=0;bool versionsOpen=false,shotsAsked=false;float scroll=0;
    for(;;)
    {
        if(releases.empty()){ShowMessageUI("Foundry","This source page is empty. Refresh the Store.",NULL);return false;}
        Pump();StoreDetails details;memset(&details,0,sizeof(details));StoreDetailsState state=title.art?GetStoreDetails(title.art,&details):STORE_DETAILS_NONE;
        if(state==STORE_DETAILS_READY&&!shotsAsked){for(int i=0;i<details.screenshotCount;++i)RequestStoreArt(title.art,STORE_ART_SCREEN+details.screenshots[i]);shotsAsked=true;}
        std::vector<StoreVersionView> versions(releases.size());for(size_t i=0;i<releases.size();++i){versions[i].label=releases[i].name.c_str();versions[i].detail="Foundry source / disc";versions[i].size=releases[i].size.c_str();}
        StoreGameView view;memset(&view,0,sizeof(view));view.titleId=title.art;view.name=title.name.c_str();view.meta=state==STORE_DETAILS_READY?details.developer:title.genre.c_str();view.players=details.players;view.rating=details.rating;view.ratings=details.ratings;
        view.description=details.description[0]?details.description:"Choose a version or disc from your server. Artwork is a catalog hint; installation checks the actual package identity.";view.loading=state==STORE_DETAILS_LOADING;
        view.screenshots=details.screenshots;view.screenshotCount=details.screenshotCount;
        view.buttons[0]=Active()?"View active download":"Install from Foundry";view.buttons[1]="Versions / discs";view.buttons[2]="DLC / add-ons";view.buttons[3]="Download queue";view.buttonDisabled[2]=title.kind=="digital";
        view.install=Active()?STORE_INSTALL_INSTALLING:STORE_INSTALL_AVAILABLE;QueueJobSnapshot snap;view.installFraction=SnapshotFoundryJobs(&snap,1)?snap.fraction:-1;
        view.versions=&versions[0];view.versionCount=(int)versions.size();view.versionChosen=chosen;view.versionsOpen=versionsOpen;view.versionFocus=versionFocus;view.versionScroll=versionScroll;view.scroll=scroll;view.focus=focus;view.focused=true;
        UiHint hints[]={{UI_BUTTON_A,L"Select",NULL},{UI_BUTTON_B,L"Back",NULL},{UI_BUTTON_LBRB,L"More source pages",L"Pages"}};
        RenderStoreGameFrame(view,hints,3);scroll=view.scroll;versionScroll=view.versionScroll;
        UiInput input=PollUiInput();if(input.pressed&XINPUT_GAMEPAD_B){if(versionsOpen)versionsOpen=false;else{ResyncUiInput();return false;}}
        else if(input.pressed&(XINPUT_GAMEPAD_LEFT_SHOULDER|XINPUT_GAMEPAD_RIGHT_SHOULDER))
        {std::string page=(input.pressed&XINPUT_GAMEPAD_RIGHT_SHOULDER)?next:previous;if(!page.empty()&&LoadReleases(title,page,&releases,&next,&previous)){cursor=page;chosen=versionFocus=versionScroll=0;}ResyncUiInput();}
        else if(versionsOpen)
        {if(input.nav==XINPUT_GAMEPAD_DPAD_UP&&versionFocus>0)--versionFocus;if(input.nav==XINPUT_GAMEPAD_DPAD_DOWN&&versionFocus+1<(int)releases.size())++versionFocus;if(input.pressed&XINPUT_GAMEPAD_A){chosen=versionFocus;versionsOpen=false;}}
        else if(input.pressed&XINPUT_GAMEPAD_A)
        {
            if(focus==0){if(Active())return true;FoundryInstallFromStore(releases[chosen].id,title.name,games,content,title.kind=="digital",forTitle);ResyncUiInput();if(Active())return true;}
            else if(focus==1){versionsOpen=true;versionFocus=chosen;}
            else if(focus==2&&title.kind!="digital"){if(OpenFoundryDlcUI(games,content,title.name.c_str(),title.art))return true;ResyncUiInput();}
            else if(focus==3)return true;
        }
        else if(input.nav==XINPUT_GAMEPAD_DPAD_LEFT&&focus>0)--focus;
        else if(input.nav==XINPUT_GAMEPAD_DPAD_RIGHT&&focus<3)++focus;
        else if(input.nav==XINPUT_GAMEPAD_DPAD_DOWN)scroll+=100;
        else if(input.nav==XINPUT_GAMEPAD_DPAD_UP)scroll=scroll>100?scroll-100:0;
        Sleep(16);
    }
}
static bool Browse(const char *games,const char *content,const std::string &kind,char letter,const std::string &initialQuery,const std::string &forTitle,const std::string &parent)
{
    std::string cursor,next,previous,query=initialQuery;std::vector<Title> titles;std::vector<StoreTileView> tiles;bool reload=true;int selected=0,scroll=0;
    for(;;)
    {
        if(reload){cJSON *reply=NULL;std::string path="/store/titles?kind="+kind+"&letter="+Encode(std::string(1,letter))+"&q="+Encode(query)+"&cursor="+(cursor.empty()?"0":cursor);
            if(!forTitle.empty())path+="&title_id="+Encode(forTitle);if(!parent.empty())path+="&parent="+Encode(parent);
            if(!FoundryStoreRequest(path,&reply))return false;titles=Titles(cJSON_GetObjectItemCaseSensitive(reply,"items"));next=Text(reply,"next_cursor");previous=Text(reply,"previous_cursor");cJSON_Delete(reply);Tiles(titles,&tiles);reload=false;selected=scroll=0;}
        Pump();StoreLetterView view;memset(&view,0,sizeof(view));view.letter=letter;view.title=query.empty()?NULL:query.c_str();view.section=kind=="dlc"?"Foundry DLC for this game":kind=="digital"?"Foundry XBLA / DLC":"Foundry games";view.filter="LB / RB: previous / next page";view.tiles=tiles.empty()?NULL:&tiles[0];view.count=(int)tiles.size();view.selected=selected;view.scroll=scroll;view.focused=true;
        UiHint hints[]={{UI_BUTTON_A,L"Open game",NULL},{UI_BUTTON_B,L"Back",NULL},{UI_BUTTON_Y,L"Search",NULL},{UI_BUTTON_LBRB,L"Pages",NULL}};
        RenderStoreLetterFrame(view,hints,4);scroll=view.scroll;UiInput input=PollUiInput();int cols=StoreGridColumns();if(cols<1)cols=1;
        if(input.pressed&XINPUT_GAMEPAD_B){ResyncUiInput();return false;}
        else if(input.pressed&XINPUT_GAMEPAD_Y){if(Search(&query)){letter='*';cursor.clear();reload=true;}}
        else if((input.pressed&XINPUT_GAMEPAD_RIGHT_SHOULDER)&&!next.empty()){cursor=next;reload=true;}
        else if((input.pressed&XINPUT_GAMEPAD_LEFT_SHOULDER)&&!previous.empty()){cursor=previous;reload=true;}
        else if((input.pressed&XINPUT_GAMEPAD_A)&&!titles.empty()){if(Game(titles[selected],games,content,forTitle))return true;ResyncUiInput();}
        else if(input.nav==XINPUT_GAMEPAD_DPAD_LEFT&&selected>0)--selected;
        else if(input.nav==XINPUT_GAMEPAD_DPAD_RIGHT&&selected+1<(int)titles.size())++selected;
        else if(input.nav==XINPUT_GAMEPAD_DPAD_UP)selected=selected>=cols?selected-cols:0;
        else if(input.nav==XINPUT_GAMEPAD_DPAD_DOWN&&!titles.empty()){selected+=cols;if(selected>=(int)titles.size())selected=(int)titles.size()-1;}
        Sleep(16);
    }
}
}

bool OpenFoundryStoreUI(const char *games,const char *content)
{
    std::vector<Title> featured;std::vector<Row> rows;int zone=0,place=0;float scroll=0;bool reload=true;
    for(;;)
    {
        if(reload){cJSON *reply=NULL;if(!FoundryStoreRequest("/store/home",&reply))return false;featured=Titles(cJSON_GetObjectItemCaseSensitive(reply,"featured"));if(featured.size()>4)featured.resize(4);rows.clear();const cJSON *array=cJSON_GetObjectItemCaseSensitive(reply,"rows");
            for(int i=0;cJSON_IsArray(array)&&i<cJSON_GetArraySize(array)&&i<STORE_ROWS_MAX;++i){Row row;const cJSON *o=cJSON_GetArrayItem(array,i);row.name=Text(o,"name");row.titles=Titles(cJSON_GetObjectItemCaseSensitive(o,"items"));if(!row.titles.empty())rows.push_back(row);}cJSON_Delete(reply);for(size_t i=0;i<rows.size();++i)Tiles(rows[i].titles,&rows[i].tiles);for(size_t i=0;i<featured.size();++i)if(featured[i].art){RequestStoreDetails(featured[i].art);RequestStoreArt(featured[i].art,STORE_ART_BACKGROUND);}reload=false;zone=featured.empty()?1:0;place=0;}
        Pump();StorePageView view;memset(&view,0,sizeof(view));view.featuredLabel="Your Foundry library";view.featuredCount=(int)featured.size();for(size_t i=0;i<featured.size();++i){view.featured[i].titleId=featured[i].art;view.featured[i].name=featured[i].name.c_str();view.featured[i].detail="Available from your server";}
        const char *buttons[]={"Games","XBLA / DLC","Download queue","Server / pairing"};for(int i=0;i<4;++i)view.buttons[i].label=buttons[i];view.letters=letters;view.rowCount=(int)rows.size();
        for(size_t i=0;i<rows.size();++i){view.rows[i].name=rows[i].name.c_str();view.rows[i].tiles=&rows[i].tiles[0];view.rows[i].count=(int)rows[i].tiles.size();view.rows[i].scroll=rows[i].scroll;}
        view.focus=zone==0?STORE_FOCUS_FEATURED+place:zone==1?STORE_FOCUS_BUTTONS+place:zone==2?STORE_FOCUS_LETTERS+place:STORE_FOCUS_ROWS+(zone-3)*STORE_ROW_TILES_MAX+place;view.scroll=scroll;view.focused=true;
        UiHint hints[]={{UI_BUTTON_A,L"Open",NULL},{UI_BUTTON_B,L"Home",NULL},{UI_BUTTON_Y,L"Search",NULL},{UI_BUTTON_X,L"Refresh",NULL}};RenderStoreFrame(view,hints,4);scroll=view.scroll;for(size_t i=0;i<rows.size();++i)rows[i].scroll=view.rows[i].scroll;
        UiInput input=PollUiInput();
        if(input.pressed&XINPUT_GAMEPAD_B){ResyncUiInput();return false;}
        else if(input.pressed&XINPUT_GAMEPAD_X)reload=true;
        else if(input.pressed&XINPUT_GAMEPAD_Y){std::string query;if(Search(&query)&&Browse(games,content,"game",'*',query))return true;ResyncUiInput();}
        else if(input.pressed&XINPUT_GAMEPAD_A)
        {
            if(zone==0&&!featured.empty()){if(Game(featured[place],games,content))return true;}
            else if(zone==1){if(place==2)return true;if(place==3){OpenFoundryUI(games,content);reload=true;}else if(Browse(games,content,place==1?"digital":"game",'*',""))return true;}
            else if(zone==2){if(Browse(games,content,"game",letters[place],""))return true;}
            else if(zone>=3&&zone-3<(int)rows.size()){if(Game(rows[zone-3].titles[place],games,content))return true;}
            ResyncUiInput();
        }
        else
        {
            int count=zone==0?(int)featured.size():zone==1?4:zone==2?28:(int)rows[zone-3].titles.size();
            int letterCols=StoreLettersPerRow();if(letterCols<1)letterCols=1;
            if(input.nav==XINPUT_GAMEPAD_DPAD_LEFT&&place>0)--place;
            else if(input.nav==XINPUT_GAMEPAD_DPAD_RIGHT&&place+1<count)++place;
            else if(input.nav==XINPUT_GAMEPAD_DPAD_UP){if(zone==2&&place>=letterCols)place-=letterCols;else if(zone>0){--zone;place=0;if(zone==0&&featured.empty())zone=1;}}
            else if(input.nav==XINPUT_GAMEPAD_DPAD_DOWN){if(zone==2&&place+letterCols<28)place+=letterCols;else if(zone<2+(int)rows.size()){++zone;place=0;}}
        }
        Sleep(16);
    }
}

bool OpenFoundryDlcUI(const char *games,const char *content,const char *name,unsigned long titleId)
{
    if(!titleId){ShowMessageUI("DLC identity","This catalog game has no Title ID.","Open the installed game's Library page for a checked DLC installation.");return false;}
    char id[9]="";if(titleId)_snprintf(id,sizeof(id),"%08lX",titleId);id[sizeof(id)-1]=0;
    return Browse(games,content,"dlc",'*',"",id,name?name:"");
}
