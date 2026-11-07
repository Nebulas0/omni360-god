// Export existing upstream artwork/catalog hints, not installation identities.
#define STORE_TITLES_DATA
#define STORE_ROWS_DATA
#define XBLA_TITLES_DATA
#include "../src/StoreTitles.h"
#include "../src/StoreRows.h"
#include "../src/XblaTitles.h"
#include <stdio.h>
static void text(const char *s){putchar('"');for(;*s;++s){unsigned char c=*s;if(c=='"'||c=='\\')putchar('\\');if(c<32)printf("\\u%04x",c);else putchar(c);}putchar('"');}
int main(){puts("[");bool first=true;
 for(int i=0;i<STORE_GAME_COUNT;++i){const StoreGame &g=kStoreGames[i];if(!first)puts(",");first=false;
  printf("{\"name\":");text(g.name);printf(",\"title_id\":\"%08lX\",\"kind\":\"game\"",g.titleId);
  for(int j=0;j<STORE_INFO_COUNT;++j)if(kStoreGameInfo[j].titleId==g.titleId){const StoreGameInfo &info=kStoreGameInfo[j];printf(",\"ratings\":%lu,\"stars\":%u,\"genre\":",info.ratings,info.stars);text(info.genre?kStoreGenres[info.genre-1]:"");break;}
  printf(",\"aliases\":[");bool comma=false;
  for(int v=0;v<g.versionCount;++v){const StoreRelease &r=kStoreReleases[g.firstVersion+v];for(int d=0;d<r.discCount;++d){if(comma)putchar(',');comma=true;text(kStoreDiscs[kStoreReleaseDiscs[r.firstDisc+d]].zip);}}
  printf("]}");
 }
 for(int i=0;i<XBLA_GAME_COUNT;++i){const XblaGame &g=kXblaGames[i];puts(",");printf("{\"name\":");text(g.name);printf(",\"title_id\":\"%08lX\",\"kind\":\"digital\",\"aliases\":[",g.titleId);text(g.rar);printf("]}");}
 puts("]");return 0;}
